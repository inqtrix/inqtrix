"""The cognitive kernel algorithm.

``mode=agent_kernel`` is a SECOND registered ``AgentAlgorithm`` next to
the deterministic phase machine: an LLM tool-calling loop
on deepagents, executed strictly through the platform seams — same
RunService -> queue -> worker path, park/resume against control rows,
sync ``graph.stream`` only, shared checkpointer with
``thread_id=run_id``.

Tool surface: the ``ask_user`` tool with a full park/resume round trip,
read tools, policy gates (compiled ``interrupt_on`` variants), canvas
writes, and child runs. The system prompt is built by
``build_agent_kernel_system_prompt``.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import re
import threading
from dataclasses import replace as dataclass_replace
from collections.abc import Callable, Collection, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from inqtrix.prompt_language import resolve_answer_language
from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.agents.checkpoint_guard import ensure_checkpoint_restart_safe
from inqtrix.agents.algorithm import (
    ARTIFACT_UPDATED_EVENT,
    CANVAS_CONTEXT_ATTACHED_EVENT,
    REQUIREMENT_ATTACHED_EVENT,
)
from inqtrix.agents.clarification import run_clarifications
from inqtrix.agents.control_ports import (
    ApprovalNotFound,
    ApprovalRecord,
    artifact_event_payload,
)
from inqtrix.agents.kernel.chat_bridge import KERNEL_USAGE_STATE, REJECTED_TOOL_ARGUMENTS_FLAG, build_tool_chat_model
from inqtrix.agents.context_sources import (
    SourceCoverageLedger,
    coverage_records_from_artifact,
)
from inqtrix.agents.kernel.deps import (
    KernelDeps,
    kernel_deps,
    run_coro,
    set_kernel_deps,
)
from inqtrix.agents.kernel.interrupts import (
    CHILDREN_INTERRUPT,
    CLARIFICATION_INTERRUPT,
    TOOL_APPROVAL_INTERRUPT,
    translate_kernel_interrupt,
)
from inqtrix.agents.kernel.policy import interrupt_config_for
from inqtrix.agents.kernel.review_pass import (
    AwaitingReview,
    ModelCall,
    ReviewHost,
    ReviewSubject,
    assess_review,
    findings_activity,
    located_results,
    marked_results,
    report_facts,
    request_review,
    revise_outputs,
    revision_state,
    unapplied_activity,
    unavailable_activity,
    waived_activity,
)
from inqtrix.agents.kernel.tools import (
    TOOL_FINISHED_EVENT,
    TOOL_STARTED_EVENT,
    build_kernel_tools,
    failed_tool_message,
    kernel_child_scope,
)
from inqtrix.agents.kernel.work_profiles import (
    RELEASED_SOURCE_KINDS,
    REVIEW_NODE,
    WORK_RESULT_BUDGET_EXHAUSTED,
    WORK_RESULT_COMPLETED,
    WORK_RESULT_MISSING,
    WORK_RESULT_SCHEMA_INVALID,
    WorkProfile,
    build_work_result,
    released_pairs,
    render_handover,
    role_source_count,
    resolve_work_profile,
    validate_work_profiles,
)
from inqtrix.agents.limit_contract import (
    LIMIT_CHOICE_CANCEL,
    LIMIT_CHOICE_PARTIAL,
    LIMIT_DECIDED_EVENT,
    LIMIT_REACHED_EVENT,
    AgentLimitGate,
    LimitChoice,
    create_or_get_limit_gate,
    disproportionate_budget,
    effective_extended_limit,
    effective_tool_grants,
    latest_terminal_limit_choice,
    next_extended_limit,
)
from inqtrix.agents.markdown import (
    normalize_agent_markdown,
    normalize_writer_markdown,
)
from inqtrix.citation_labels import (
    AGENT_FAMILIES,
    citation_pattern,
    mark_malformed_citations,
)
from inqtrix.agents.narration import emit_narration
from inqtrix.agents.run_outcome import build_outcome, cancel_final, send_outcome
from inqtrix.agents.patterns._structured import observe_structured_retries
from inqtrix.agents.telemetry import model_retry_activity, provider_retry_activity
from inqtrix.agents.skills_runtime import (
    skill_model_pins,
    strictest_requires_plan,
)
from inqtrix.agents.tier_policy import (
    VERIFY_LEVELS,
    resolve_verify_level,
    review_required,
)
from inqtrix.agents.web_execution_policy import derive_web_research_policy
from inqtrix.execution_authority import pinned_knowledge_collection_ids
from inqtrix.i18n import detect_ui_language, t, t_count
from inqtrix.model_routing import (
    describe_resolution,
    describe_unresolved_resolution,
)
from inqtrix.observability.context import bind_purpose, current_purpose
from inqtrix.providers.base import observe_provider_retries, provider_attempt_timeout, provider_cancel_scope
from inqtrix.urls import normalize_url, scrub_credential_urls

if TYPE_CHECKING:
    from inqtrix.agents.checkpointing import CheckpointerHandle
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.core.context import RunContext, RuntimeContext
    from inqtrix.core.results import AgentResult, RunRequest, WorkHandover
    from inqtrix.providers.base import ChatTurn
    from inqtrix.settings import AgentPlatformSettings

log = logging.getLogger("inqtrix")

#: Tools whose approval card names the run's knowledge boundary.
_KNOWLEDGE_SCOPED_TOOLS = frozenset(
    {"search_project_knowledge", "read_project_document"}
)


def _knowledge_scope_names(
    deps: KernelDeps,
    actions: list[dict[str, Any]],
) -> list[str] | None:
    """Collection names the run's knowledge boundary admits.

    Returns ``None`` when the gate has nothing to do with knowledge,
    or when the catalog cannot be read — an unreadable catalog omits
    the line rather than inventing a name.
    """
    if not any(
        action.get("tool") in _KNOWLEDGE_SCOPED_TOOLS
        for action in actions
    ):
        return None
    registry = getattr(deps, "capability_registry", None)
    if registry is None:
        return None
    try:
        output = run_coro(
            registry.invoke(
                "knowledge.collections.list",
                {},
                deps.capability_context,
            )
        )
    except Exception:  # noqa: BLE001 - catalog is advisory here
        log.warning(
            "Knowledge scope for approval could not be read; collections are not named "
            "on the gate card."
        )
        return None
    return sorted(
        str(collection.name)
        for collection in getattr(output, "collections", [])
    )


_KERNEL_NODE = "agent_kernel"

_WAITING_STATUS_BY_ORIGIN = {
    CLARIFICATION_INTERRUPT: "waiting_for_input",
    TOOL_APPROVAL_INTERRUPT: "waiting_for_approval",
    CHILDREN_INTERRUPT: "waiting_for_children",
}

PHASE_CHANGED_EVENT = "inqtrix.agent.phase.changed"
TODO_UPDATED_EVENT = "inqtrix.agent.todo.updated"

#: Character cap of a tool-result preview. A result is a raw dump that can
#: carry a whole document body (a read returns the text it read), so it
#: stays tight — the event log must not become a second copy of every
#: artifact.
_RESULT_PREVIEW_LIMIT = 200

#: Character cap of a preview that is PROSE. Prose is what a reader
#: actually sees in the transcript, and 200 cut delegation assignments
#: mid-sentence, so it gets more room. Nothing bulky reaches this lane:
#: only the named intent arguments below do.
_PROSE_PREVIEW_LIMIT = 400

#: Argument names that carry the human-readable INTENT of a tool call,
#: in the order a preview prefers them. A call without any of them has no
#: preview.
_PREVIEW_INTENT_ARGS = (
    "query",
    "question",
    "assignment",
    "objective",
    "instruction",
    "title",
)
"""Argument names that carry a tool call's INTENT, in precedence order.
The transcript's tool row shows this value while the tool runs, so every
tool with a prose argument must be listed: ``run_web_research`` asks a
``question``, ``write_canvas`` names a ``title`` — an unlisted one
shows no preview at all."""


def _visible_clip(text: str, limit: int = _RESULT_PREVIEW_LIMIT) -> str:
    """Bounded preview with a VISIBLE cut marker (no silent caps)."""
    return text if len(text) <= limit else text[:limit] + "…"


def _args_preview(args: dict[str, Any]) -> str:
    """One readable line describing what a tool call was asked to do.

    The preview is what the transcript SHOWS a user while a tool runs, so
    it has to survive the length cap AS PROSE. Serialising first and
    clipping afterwards produced a truncated JSON dump — unreadable for
    the reader, and unparseable for the surface, which then rendered the
    rubble verbatim. Only ``web_instant`` escaped it, because it alone
    takes a single argument named ``query``.

    So the intent argument is chosen BEFORE the cut: the clip lands on a
    sentence, never on a serialization. A call without such an argument
    (an id-only read) has no preview: an identifier is no words for the
    reader, so the row shows the tool alone.
    """
    for name in _PREVIEW_INTENT_ARGS:
        value = args.get(name)
        if isinstance(value, str) and value.strip():
            return _visible_clip(value.strip(), _PROSE_PREVIEW_LIMIT)
    return ""


def _evidence_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    """Name a registered selection without fetching or validating source content."""
    reference_id = args.get("reference_id")
    query_id = args.get("query_id")
    reference_id = reference_id.strip() if isinstance(reference_id, str) else ""
    query_id = query_id.strip() if isinstance(query_id, str) else ""
    title = ""
    with deps._evidence_lock:
        if reference_id and not query_id:
            reference = deps.evidence_refs.get(reference_id, {})
            title = reference.get("title") or reference.get("url") or ""
        elif query_id and not reference_id:
            search = (deps.web_search_ledger.get("searches") or {}).get(query_id, {})
            title = search.get("query") or ""
    if isinstance(title, str) and title.strip():
        return _args_preview({"title": title})
    return _args_preview(args)


def _report_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    """Name an attached research report by its title, never by its run id."""
    report_id = args.get("report_id")
    title = deps.attached_report_titles.get(report_id) if isinstance(report_id, str) else None
    return _args_preview({"title": title}) if isinstance(title, str) else ""


def _context_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    if args.get("query"):
        return _args_preview(args)
    words = {"language": resolve_answer_language(deps.question).code}
    if args.get("source_id"):
        return t(words, "kernel_context_source_read_preview")
    return t(words, "kernel_context_source_list_preview")


def _is_run_archive(artifact_id: str, deps: KernelDeps) -> bool:
    """Whether a canvas id names the run's own archive (catalogue or entry)."""
    prefix = deps.context_archive_prefix
    return artifact_id == prefix or artifact_id.startswith(prefix + "_")


def _canvas_document_id(args: dict[str, Any], deps: KernelDeps) -> str:
    """The canvas document a ``read_canvas`` call reads; "" for the run's archive."""
    artifact_id = args.get("artifact_id")
    if not isinstance(artifact_id, str) or _is_run_archive(artifact_id, deps):
        return ""
    return artifact_id


def _canvas_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    """Describe archive reads without copying identifiers or fetching content.

    A canvas document read has no words here: the started event carries its
    id as data, and the client names the document by the title it knows.
    """
    artifact_id = args.get("artifact_id")
    if not isinstance(artifact_id, str) or not _is_run_archive(artifact_id, deps):
        return ""
    prefix = deps.context_archive_prefix
    words = {"language": resolve_answer_language(deps.question).code}
    if artifact_id == prefix:
        return t(words, "kernel_archive_catalogue_next_preview" if args.get("cursor")
                 else "kernel_archive_catalogue_preview")
    offset = args.get("offset")
    if type(offset) is int and offset >= 0:
        # The tool uses zero-based offsets; the reader sees one-based positions.
        position = format(offset + 1, ",")
        if words["language"] == "de":
            position = position.replace(",", ".")
        return t(words, "kernel_archive_range_preview", position=position)
    return t(words, "kernel_archive_read_preview")


def _document_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    """Describe a knowledge or editor text window without copying identifiers.

    The reader sees where the read starts, one-based like the archive
    preview; the document id, fingerprint and version stay in the protected
    content capture.
    """
    words = {"language": resolve_answer_language(deps.question).code}
    offset = args.get("offset")
    if type(offset) is int and offset > 0:
        position = format(offset + 1, ",")
        if words["language"] == "de":
            position = position.replace(",", ".")
        return t(words, "kernel_document_range_preview", position=position)
    if args.get("comments_cursor"):
        return t(words, "kernel_document_comments_next_preview")
    return t(words, "kernel_document_read_preview")


def _cite_args_preview(args: dict[str, Any], deps: KernelDeps) -> str:
    """Describe a citation by the window it cites from and its passage count.

    The window reads like the read that brought the text in
    (:func:`_document_args_preview`); the document id, the version and the
    passages themselves stay in the protected content capture.
    """
    passages = args.get("passages")
    count = len(passages) if isinstance(passages, list) else 0
    window = _document_args_preview({"offset": args.get("window_start")}, deps)
    words = {"language": resolve_answer_language(deps.question).code}
    return t_count(words, "kernel_cite_passages_preview", count, window=window)


_SUPERSTEPS_PER_TOOL_TURN = 8
"""LangGraph super-steps consumed by ONE model turn that calls a tool.

The kernel's ``recursion_limit`` counts super-steps, not model turns, and
every middleware hook is its own graph node: ``before_model`` (skill
inputs, sufficiency) -> model -> ``after_model`` (child-batch guard, tool
budget, todo, and HITL when the policy gates) -> the tool node. Measured
against the compiled production graph: 8 per tool turn in every
conversational policy variant, because each of them gates
``propose_editor_patch`` and so carries the HITL node, which costs one
super-step per turn. A role graph compiles without it and without the
todo planner and spends two less per turn, so a role ceiling priced with
this constant errs on the safe side.

Budgets below are expressed as TOOL TURNS multiplied by this constant,
so the ceilings say what they mean. ``tests/agents/
test_harness_kernel_contract.py::test_supersteps_per_tool_turn_is_pinned``
measures the compiled graph and fails when a middleware change makes this
value wrong — the number is derived, never guessed (the sufficiency
middleware raised it from 7 to 8 exactly this way)."""

def _todo_in_progress_index(todos: list[Any]) -> int | None:
    """Index of the first ``in_progress`` item — ONE rule for the list the
    graph reports and the list a turn's ``write_todos`` call carries."""
    return next(
        (
            index
            for index, item in enumerate(todos)
            if isinstance(item, dict)
            and str(item.get("status", "")) == "in_progress"
        ),
        None,
    )


_ANSWER_TURN_SUPERSTEPS = 9
"""Super-steps of the bare final answer turn (measured alongside the
per-tool-turn price; the answer turn carries the graph's entry/exit
overhead, so it costs ONE more than a tool turn). A ceiling formula is
always ``_ANSWER_TURN_SUPERSTEPS + turns * _SUPERSTEPS_PER_TOOL_TURN``
— pinned by the same contract test."""

_SCHNELL_TOOL_TURNS = 3
"""Tool turns the ``schnell`` tier may spend BESIDE the answer turn:
the ONE published ``web_instant`` call, one knowledge/todo turn (both
allowed in schnell), and one slack turn for a failed or blocked call.
The tier publishes ``web_instant_budget=1``; a clamp that cannot afford
that call plus the answer would make every schnell tool run a
guaranteed ``GraphRecursionError`` (published != enforced) — the
pre-recalibration literal ``8`` did exactly that."""

_SCHNELL_READ_TURNS = 3
"""Read-only turns the ``schnell`` clamp affords ON TOP of
:data:`_SCHNELL_TOOL_TURNS`: reads of evidence the session already
retained (``read_evidence``: one index call plus one call per selected
reference), canvas documents and attached reports. A follow-up turn in a
session with earlier searches reads before it answers; reads cost no
source budget and are bounded here, so the clamp keeps the tier's search
cost class (the source budgets of the tier table) while affording them."""

def _emit_kernel_model_retry(
    deps: KernelDeps,
    node: str,
    notice: dict[str, Any],
) -> None:
    """Emit one Kernel model retry through the shared activity channel."""
    deps.emit(
        "inqtrix.agent.activity",
        model_retry_activity(
            notice,
            node=node,
            language=resolve_answer_language(deps.question).code,
        ),
    )


class _DepsChatProvider:
    """Duck-typed ``LLMProvider.chat`` delegate reading kernel deps.

    The compiled kernel graph (and thus its bound chat model) is shared
    across runs, but model override, effort, and timeout are PER
    SEGMENT — this delegate resolves them from the deps ContextVar at
    call time, so one compiled graph serves every run and stack. The
    model-call boundary doubles as the kernel's abort chokepoint
    (cancel event + token budget); usage booking happens ONCE via the
    bridge's ``usage_hook``, not here.
    """

    def chat(
        self,
        messages: Any,
        *,
        tools: Any = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout: float = REASONING_TIMEOUT,
        **kwargs: Any,
    ) -> "ChatTurn":
        deps = kernel_deps()
        deps.check_abort()
        bound_purpose = current_purpose()
        purpose = (
            "agent_context_projection"
            if bound_purpose is not None and bound_purpose.purpose == "agent_context_projection"
            # A role's model turns carry the purpose of its routing node, so
            # the review stays findable (and billed) as the review it is.
            else deps.work_profile.routing_node
            if deps.work_profile is not None
            else "agent_kernel"
        )
        with provider_cancel_scope(
            deps.cancel_token.is_set if deps.cancel_token is not None else None
        ), observe_provider_retries(
            deps.llm,
            lambda notice: _emit_kernel_model_retry(
                deps, purpose, notice
            ),
        ), bind_purpose(purpose):
            return deps.llm.chat(
                messages,
                tools=tools,
                model=deps.model,
                reasoning_effort=deps.reasoning_effort,
                timeout=provider_attempt_timeout(deps.timeout),
                **kwargs,
            )

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True


def _review_model_call(deps: KernelDeps) -> ModelCall:
    """The review call's own routing — the review node, the user's choice.

    Read from ``model_overrides`` rather than the run's resolved model:
    the overrides carry the request's and the skill's selection BEFORE the
    Deep effort elevation, and ``model_routing`` states for this node that
    the review follows the selected model and effort while the kernel's
    automatic increase stays local to the reasoning loop and the revision.
    """
    provider_models = getattr(deps.llm, "models", None)
    requested_tier = deps.model_overrides.get("model_tier", "")
    requested_model = deps.model_overrides.get("model", "")
    if provider_models is None and not requested_model:
        desc = describe_unresolved_resolution(REVIEW_NODE, requested_tier)
    else:
        desc = describe_resolution(
            REVIEW_NODE,
            provider_models,
            requested_tier,
            requested_model=requested_model,
            requested_effort=deps.model_overrides.get("effort", ""),
        )
    deps.emit("inqtrix.node.model_resolution", desc)
    return ModelCall(
        REVIEW_NODE, desc.get("model") or None, desc.get("effort") or None
    )


def _kernel_review_report(
    facts: Mapping[str, Any], *, by_cancel: bool
) -> dict[str, Any]:
    """This segment's review in the review-report shape the record reads.

    The mission's shape (``critic`` beside ``last_review``), built by the
    same :func:`~inqtrix.agents.kernel.review_pass.report_facts`, so both
    engines count their open points alike; a kernel run reviews once and
    has no research limits and no prospect. A located finding no revision
    decided is named by the exit that stopped it: ``cancelled`` when a
    cancel did (*by_cancel*), ``revision_error`` when an error did. The
    statements are counted on the texts the segment delivers
    (``facts["outputs"]``).
    """
    assessment = facts.get("assessment")
    if assessment is None:
        return {"last_review": dict(facts["last_review"])} if facts.get("last_review") else {}
    settled = list(facts["results"])
    decided = {result.finding_id for result in settled}
    unsettled = (
        located_results(assessment, "cancelled")
        if by_cancel
        else located_results(assessment, "revision_error")
    )
    results = [*settled, *(result for result in unsettled if result.finding_id not in decided)]
    return {
        "critic": {
            "round": 1,
            "research_limits": [],
            "prospect_codes": [],
            **report_facts(
                assessment,
                results,
                revision=revision_state(results),
                language=facts["language"],
                outputs=facts.get("outputs"),
            ),
        },
        "last_review": dict(facts["last_review"]),
    }


def _written_outputs(
    outputs: Mapping[str, str], revised: Mapping[str, str], written: Collection[str]
) -> dict[str, str]:
    """The texts a segment delivers once its revision is written: the revised
    answer and every canvas the revision wrote; every other canvas as the
    review read it, since an unchanged canvas is not written again."""
    return {
        **outputs,
        "chat": revised["chat"],
        **{artifact_id: revised[artifact_id] for artifact_id in written},
    }


def _send_kernel_outcome(
    deps: KernelDeps, *, final: str, by_cancel: bool = False
) -> None:
    """Send this segment's outcome record (:mod:`inqtrix.agents.run_outcome`), once.

    ``final`` is the kernel's decision at this exit; *by_cancel* says a
    cancel stopped the segment (:func:`_kernel_review_report`). A role run
    sends none: it is a child run whose record would cross to its parent.
    The flag keeps a later exit of the same segment (an error after the
    record, a failed checkpoint delete) from sending a second one; a
    redelivered segment may send it again under the same ``outcome_id``,
    and readers take the last.
    """
    if deps.work_profile is not None or deps.outcome_sent:
        return
    deps.outcome_sent = True
    send_outcome(
        deps.emit,
        lambda: {
            **_kernel_review_report(deps.review_facts, by_cancel=by_cancel),
            "language": _answer_language(deps),
        },
        run_id=deps.run_id,
        final=final,
        guard_for=lambda _values: deps.guard_reader_text,
    )


def _answer_language(deps: KernelDeps) -> str:
    """The language of the run's answer: the one its review narration and
    its outcome record speak."""
    return resolve_answer_language(deps.question).code or ""


def _with_outcome_on_failure(
    deps: KernelDeps, call: Callable[[], "AgentResult"]
) -> "AgentResult":
    """Run one exit path of a segment; an error leaving it still sends the record.

    A cancel reads as the kernel's cancel decision (:func:`cancel_final`),
    anything else as ``failed``; the error then goes on unchanged.
    """
    from inqtrix.exceptions import AgentCancelled

    try:
        return call()
    except AgentCancelled as exc:
        _send_kernel_outcome(deps, final=cancel_final(exc), by_cancel=True)
        raise
    except Exception:
        _send_kernel_outcome(deps, final="failed")
        raise


class KernelAgentAlgorithm:
    """The conversational kernel over the platform seams.

    Args:
        control_store: Clarification/approval rows (park truth).
        checkpointer: The checkpointer handle; REQUIRED — the kernel
            never registers without one (container gate).
        platform: Server-side agent limits (never prompted).
        capability_registry: The capability tool surface; ``None``
            degrades every capability tool to
            a visible not-available result.
        permission_service: Resolves the owner's visibility context per
            segment (the agent's tools see exactly what its OWNER
            sees); ``None`` keeps the historical unscoped view of the
            anonymous/static modes.
        run_service: Child-run submission seam (research fan-out +
            deep-mission delegation); ``None`` degrades the child tools
            to a visible not-available result.
        resolver: Builds child ``ResolvedAgentContext``s; required
            together with *run_service*.
    """

    id = "agent_kernel"
    display_name = "Agent-Kernel"

    def __init__(
        self,
        *,
        control_store: "AgentControlStore",
        checkpointer: "CheckpointerHandle",
        platform: "AgentPlatformSettings",
        capability_registry: Any = None,
        permission_service: Any = None,
        run_service: Any = None,
        resolver: Any = None,
        skill_service: Any = None,
        agent_memory_service: Any = None,
        source_service: Any = None,
    ) -> None:
        self._control = control_store
        self._checkpointer = checkpointer
        self._platform = platform
        self._capabilities = capability_registry
        self._permissions = permission_service
        self._run_service = run_service
        self._resolver = resolver
        self._skill_service = skill_service
        self._sources = source_service
        # The SAME long-term memory service the mission engine uses:
        # one memory stack, opt-in per user, never evidentiary.
        self._memory = agent_memory_service
        # One compiled graph per policy: interrupt_on is compile-time
        # and the policy is fixed per run.
        self._graphs: dict[tuple[str, str, str, int, str], Any] = {}
        self._graph_lock = threading.Lock()
        validate_work_profiles(
            str(getattr(tool, "name", "")) for tool in build_kernel_tools()
        )

    def capabilities(self) -> dict[str, Any]:
        """Registry manifest entry (no research-graph streaming)."""
        return {
            "requires": ["llm"],
            "streams_events": True,
            "supports_chat_completions": False,
            "terminal_node": _KERNEL_NODE,
            "produces": ["markdown"],
            "interrupts": ["clarification", "approval"],
        }

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        request: "RunRequest",
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> "AgentResult":
        """Execute or RESUME one kernel run (segment-aware)."""
        from inqtrix.core.results import AgentResult

        if context.run_id is None or context.park is None:
            raise RuntimeError(
                ("agent_kernel runs only through /v1/runs (parking support and run_id "
                "required).")
            )
        llm = context.providers.llm
        if not llm.supports_tool_calls():
            # Per-request stacks may resolve a different provider than
            # the registration-time default — fail loudly, never degrade
            # to a tool-less loop.
            raise RuntimeError(
                ("The resolved LLM provider does not support native tool calling; "
                "mode=agent_kernel is unavailable for this stack.")
            )
        run_id = context.run_id
        autonomy = (
            request.autonomy or self._platform.default_autonomy
        ).strip() or "balanced"
        depth = context.agent_settings.depth
        profile = resolve_work_profile(request.work_profile)
        if profile is not None and request.handover is None:
            raise RuntimeError(
                f"Role run '{profile.name}' has no handover; refusing to run "
                "it as a conversation."
            )
        skill_records = self._load_request_skills(request, context)
        pin_tier, pin_effort = skill_model_pins(skill_records)
        deps = self._build_deps(
            run_id,
            llm,
            context,
            request,
            pin_tier=pin_tier,
            pin_effort=pin_effort,
            depth=depth,
            node=profile.routing_node if profile is not None else _KERNEL_NODE,
        )
        deps.depth = depth
        deps.work_profile = profile
        deps.released_sources = released_pairs(request.handover)
        deps.question = scrub_credential_urls(request.question)
        deps.request_user_text = _request_user_text(request)
        deps.report_requirement = request.report_requirement or ""
        deps.report_rules = tuple(request.report_rules)
        deps.session_id = request.session_id or ""
        deps.run_service = self._run_service
        deps.resolver = self._resolver
        deps.principal = context.principal
        deps.autonomy = autonomy
        deps.skill_service = self._skill_service
        deps.stack_name = getattr(context, "stack_name", "") or ""
        # Long-term memory: the mission engine's service + opt-in
        # semantics — resolved once per segment; a preference-read error
        # degrades to False inside opt_in_enabled (never silently on).
        deps.memory = self._memory
        if self._memory is not None and context.principal is not None:
            deps.memory_opt_in = run_coro(
                self._memory.opt_in_enabled(context.principal)
            )
        from inqtrix.agents.source_policy import effective_source_policy

        deps.source_policy = effective_source_policy(request.source_policy)
        # The submitted scope's provenance rides the durable request, so
        # a resumed segment reads the same intent.
        deps.knowledge_scope_explicit = bool(
            request.knowledge_filters.get("explicit")
        )
        deps.knowledge_scope = request.knowledge_filters.get("scope") or None
        tier = getattr(context.agent_settings, "agent_tier", "") or ""
        research_policy = derive_web_research_policy(
            depth=depth,
            admitted_directive="web_research" in request.tool_directives,
            tier=tier or None,
        )
        deps.tier = tier
        # The review gate reads the PUBLISHED level, not the depth flag:
        # a Stufe maps onto depth for budgets, and everything below
        # "tief" maps onto "normal" — reading depth would silently waive
        # the review the Stufe table promises. A request without a Stufe
        # keeps the legacy meaning of depth.
        deps.verify_level = resolve_verify_level(
            tier, legacy="standard" if depth == "deep" else "labels"
        )
        deps.web_research_allowed = research_policy.allowed
        deps.web_research_profile = research_policy.profile
        deps.hydrate_evidence()
        if profile is not None and request.handover.evidence_run_id:
            # The released ledger, re-authorized at every segment start.
            deps.hydrate_evidence(request.handover.evidence_run_id)
        deps.capability_context = dataclass_replace(
            deps.capability_context,
            question=deps.question,
        )
        for record in skill_records:
            deps.activate_skill(record)
        if autonomy == "autonomous" and (
            strictest_requires_plan(deps.skills) == "always"
        ):
            # The kernel has no plan phase — `requires_plan: always`
            # (the one lever a skill author has to force human review)
            # maps onto the gated policy variant instead of silently
            # no-opping in Auto. Deterministic per segment: derived
            # from the persisted request skills, so every resume picks
            # the same compiled graph.
            log.info(
                "Run %s: a skill with requires_plan=always enforces the standard "
                "approval mode (autonomous -> balanced).",
                run_id,
            )
            autonomy = "balanced"
            deps.autonomy = autonomy
        (
            base_tool_limit,
            tool_ceiling,
            base_step_limit,
            step_ceiling,
        ) = configured_kernel_limits(
            self._platform,
            depth=depth,
            tier=tier,
            # Role-ness stays the profile's decision, exactly as before; the
            # handover only says how big the work is. A run without a profile
            # never derives a role budget even if a handover rode along.
            handover=request.handover if profile is not None else None,
        )
        deps.tool_call_ceiling = tool_ceiling
        deps.tool_call_limit = effective_extended_limit(
            self._control,
            run_id=run_id,
            kind="tool_calls",
            base=base_tool_limit,
            ceiling=tool_ceiling,
            run_async=run_coro,
        )
        deps.step_ceiling = step_ceiling
        deps.step_limit = effective_extended_limit(
            self._control,
            run_id=run_id,
            kind="steps",
            base=base_step_limit,
            ceiling=step_ceiling,
            run_async=run_coro,
        )
        # Run-wide tool grants fold like the limits above — durable
        # decision rows re-read at every segment start. They take effect
        # ONLY in the balanced mode table (strict stays per-call), so the
        # snapshot never advertises a grant that has no gating effect.
        deps.tool_grants = (
            effective_tool_grants(
                self._control, run_id=run_id, run_async=run_coro
            )
            if autonomy == "balanced"
            else frozenset()
        )
        graph = self._compiled_graph(
            autonomy,
            source_policy=deps.source_policy,
            max_tool_calls=deps.tool_call_limit,
            profile=profile,
        )
        result_calls = _result_call_names(profile)
        state_config = {"configurable": {"thread_id": run_id}}
        existing = graph.get_state(state_config)
        deps.hydrate_source_receipts(existing)
        deps.check_abort()
        deps.prior_usage = _checkpointed_usage(existing)
        deps.tool_use_counts = _checkpointed_tool_use_counts(existing)
        deps.web_searches = _checkpointed_web_searches(existing)
        deps.source_coverage = _checkpointed_source_coverage(existing)
        deps.tool_calls_used = _checkpointed_tool_call_count(
            existing, uncounted=result_calls
        )
        (
            deps.emitted_tool_start_ids,
            deps.emitted_tool_finish_ids,
        ) = _checkpointed_tool_event_ids(existing)
        deps.checkpointed_steps = _checkpointed_steps(existing)
        self._reactivate_loaded_skills(deps, existing)
        _recover_clarified_answers(deps, existing)
        _reactivate_editor_read_receipts(deps, existing)

        terminal_limit = latest_terminal_limit_choice(
            self._control, run_id=run_id, run_async=run_coro
        )
        if terminal_limit is not None:
            gate, choice, record = terminal_limit
            return _with_outcome_on_failure(
                deps,
                lambda: self._apply_terminal_limit_choice(
                    request,
                    deps,
                    gate=gate,
                    choice=choice,
                    clarification_id=record.clarification_id,
                    transcript_state=existing,
                ),
            )

        remaining_steps = deps.step_limit - deps.checkpointed_steps
        if remaining_steps <= 0 and bool(getattr(existing, "next", ())):
            if profile is not None:
                return self._role_result(
                    request, deps, profile, existing,
                    status=WORK_RESULT_BUDGET_EXHAUSTED,
                    detail="The role reached its step limit.",
                )
            gate = AgentLimitGate(
                kind="steps",
                current=deps.step_limit,
                proposed=next_extended_limit(
                    current=deps.step_limit,
                    ceiling=deps.step_ceiling,
                ),
                ceiling=deps.step_ceiling,
                used=deps.checkpointed_steps,
            )
            return self._park_for_limit(request, context, deps, gate=gate)

        config = {
            "configurable": {"thread_id": run_id},
            # LangGraph applies this bound to one invocation. Subtracting
            # the checkpoint's cumulative step coordinate turns it into the
            # published run-wide allowance across approvals and resumes.
            "recursion_limit": max(1, remaining_steps),
        }
        graph_input = self._graph_input(request, run_id, existing, deps)

        from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded

        def execution_snapshot() -> dict[str, Any]:
            # Built at EMISSION time so tool-boundary snapshots carry the
            # counters as they stand after the boundary, not as they stood
            # when the segment started.
            return _asserted_execution_snapshot({
                "current_node": _KERNEL_NODE,
                "phase": "execution",
                "execution": self._execution_payload(
                    request, deps, consent_reason=_kernel_consent_reason(deps)
                ),
            })

        deps.emit(
            PHASE_CHANGED_EVENT,
            {
                "phase": "execution",
                "previous_phase": "",
                "snapshot": execution_snapshot(),
            },
        )
        set_kernel_deps(deps)
        try:
            with observe_structured_retries(
                lambda node, notice: _emit_kernel_model_retry(
                    deps, node, notice
                )
            ):
                interrupts = [
                    item
                    for update in graph.stream(
                        graph_input, config=config, stream_mode="updates"
                    )
                    if isinstance(update, dict)
                    for item in _observe_update(deps, update, execution_snapshot)
                ]
        except AgentCancelled as exc:
            from inqtrix.agents.kernel.transcript import archive_checkpoint

            archive_checkpoint(deps, lambda: graph.get_state(state_config), request, required=False)
            # Cancel/budget stop at the model boundary: the segment's
            # spend is returned (execute_run_request books it before
            # cancelling), the checkpoint stays for post-mortems.
            if isinstance(exc, AgentTokenBudgetExceeded):
                token_used = sum(deps.run_usage().values())
                token_payload = {
                    "kind": "tokens",
                    "used": max(0, token_used),
                    "limit": deps.token_budget,
                    "ceiling": deps.token_budget,
                    "extendable": False,
                    "recoverable": False,
                    "reason": "operator_ceiling_exactly_once_required",
                    "state": "failed_at_model_boundary",
                }
                deps.emit(LIMIT_REACHED_EVENT, token_payload)
                emit_narration(
                    deps.emit,
                    narration_id=f"n-limit-tokens-{deps.run_id[-12:]}",
                    kind="limit",
                    text=t(
                        {"language": resolve_answer_language(deps.question).code},
                        "agent_limit_tokens_narration",
                    ),
                    phase="execution",
                    guard=deps.guard_reader_text,
                )
            _send_kernel_outcome(deps, final=cancel_final(exc), by_cancel=True)
            return AgentResult(
                answer="",
                result_type="agent_kernel_result",
                raw={
                    "answer": "",
                    "usage": dict(deps.usage),
                    "result_state": {
                        "usage_total": deps.run_usage(),
                        "cancelled": True,
                        "cancel_reason": (
                            "token_budget_exceeded"
                            if isinstance(exc, AgentTokenBudgetExceeded)
                            else "client_requested_cancel"
                        ),
                        "execution": self._execution_payload(
                            request,
                            deps,
                            consent_reason=_kernel_consent_reason(deps),
                        ),
                    },
                },
            )
        except Exception as exc:
            # Both loop ceilings stop at checkpoint-safe boundaries. They
            # therefore reuse the native clarification wait/resume contract
            # instead of becoming a generic failure or a silent partial.
            from langgraph.errors import GraphRecursionError

            from inqtrix.agents.kernel.middleware import (
                KernelToolBudgetExceeded,
            )

            if profile is not None:
                from langchain.agents.structured_output import (
                    StructuredOutputValidationError,
                )

                # A role never parks at the user: its limits and an
                # unrepairable result are outcomes the parent reads.
                failed = (
                    WORK_RESULT_BUDGET_EXHAUSTED
                    if isinstance(exc, (GraphRecursionError, KernelToolBudgetExceeded))
                    else WORK_RESULT_SCHEMA_INVALID
                    if isinstance(exc, StructuredOutputValidationError)
                    else ""
                )
                if failed:
                    log.warning(
                        "Role run %s (%s) ended without a usable result: %s.",
                        run_id, profile.name, failed,
                    )
                    return self._role_result(
                        request, deps, profile, graph.get_state(state_config),
                        status=failed, detail=str(exc),
                    )
            if isinstance(exc, GraphRecursionError):
                stopped = graph.get_state(state_config)
                deps.checkpointed_steps = max(
                    deps.step_limit, _checkpointed_steps(stopped)
                )
                return self._park_for_limit(
                    request,
                    context,
                    deps,
                    gate=AgentLimitGate(
                        kind="steps",
                        current=deps.step_limit,
                        proposed=next_extended_limit(
                            current=deps.step_limit,
                            ceiling=deps.step_ceiling,
                        ),
                        ceiling=deps.step_ceiling,
                        used=deps.checkpointed_steps,
                    ),
                )
            if isinstance(exc, KernelToolBudgetExceeded):
                deps.tool_calls_used = max(
                    0, exc.attempted - exc.batch_size
                )
                return self._park_for_limit(
                    request,
                    context,
                    deps,
                    gate=AgentLimitGate(
                        kind="tool_calls",
                        current=deps.tool_call_limit,
                        proposed=next_extended_limit(
                            current=deps.tool_call_limit,
                            ceiling=deps.tool_call_ceiling,
                            required=exc.attempted,
                        ),
                        ceiling=deps.tool_call_ceiling,
                        used=deps.tool_calls_used,
                    ),
                )
            from inqtrix.agents.kernel.transcript import archive_checkpoint

            archive_checkpoint(deps, lambda: graph.get_state(state_config), request, required=False)
            # The graph failed on its own: the record says so, and the
            # original error goes on.
            _send_kernel_outcome(deps, final="failed")
            raise
        finally:
            set_kernel_deps(None)

        # Every decision after the loop sends the segment's outcome record
        # once: the normal end and a cancelled review at their own place, any
        # error leaving the post-loop through this handler. A park and a
        # role result send none.
        return _with_outcome_on_failure(
            deps,
            lambda: self._conversation_post_loop(
                request, context, deps, graph, config, state_config, interrupts, profile
            ),
        )

    def _conversation_post_loop(
        self,
        request: "RunRequest",
        context: "RunContext",
        deps: KernelDeps,
        graph: Any,
        config: dict[str, Any],
        state_config: dict[str, Any],
        interrupts: list[Any],
        profile: WorkProfile | None,
    ) -> "AgentResult":
        """Finish one segment after its loop: park, role result, or the
        reviewed answer.

        ``AwaitingReview`` parks here and never leaves it; a cancelled
        review returns cancelled with its record.
        """
        from inqtrix.core.results import AgentResult
        from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded

        run_id = deps.run_id

        if interrupts:
            if len(interrupts) > 1:
                # Parallel gates have no defined resume mapping yet —
                # parking would strand the run half-answered (loud
                # failure is safer than an undefined resume).
                raise RuntimeError(
                    ("Multiple parallel kernel interrupts in one segment are not "
                    "supported.")
                )
            origin, payload = translate_kernel_interrupt(
                interrupts[0].value
            )
            if origin == TOOL_APPROVAL_INTERRUPT:
                self._ensure_tool_approval(
                    run_id,
                    deps,
                    interrupt_id=str(interrupts[0].id),
                    payload=payload,
                )
            context.park(_WAITING_STATUS_BY_ORIGIN[origin])
            return self._parked_result(request, deps)

        state = graph.get_state(config)
        if profile is not None:
            return self._role_result(request, deps, profile, state)
        deps.checkpointed_steps = _checkpointed_steps(state)
        deps.tool_calls_used = _checkpointed_tool_call_count(state)
        answer = _final_answer(state.values.get("messages") or [])
        if not answer:
            from inqtrix.agents.kernel.transcript import archive_checkpoint

            archive_checkpoint(deps, state, request)
            self._checkpointer.delete_thread(run_id)
            raise RuntimeError(
                ("The kernel finished without answer text (finish without an AI "
                "message).")
            )
        canvases = self._current_run_canvases(deps)
        deps.effective_response_form = "canvas" if canvases else "chat"
        if deps.verify_level not in VERIFY_LEVELS:
            raise RuntimeError(
                "The kernel segment reached the review gate without a "
                f"resolved report-quality level (got {deps.verify_level!r}); "
                "an unresolved level must not read as 'nothing to verify'."
            )
        if review_required(deps.verify_level):
            # BEFORE delete_thread: a redelivered segment that died
            # inside the verify pass must still find its checkpoint.
            try:
                with observe_structured_retries(
                    lambda node, notice: _emit_kernel_model_retry(
                        deps, node, notice
                    )
                ):
                    # Inside the review's try: a cancel during the draft's
                    # citation check ends the run like a cancel in the review.
                    _show_answer_draft(deps, answer)
                    answer = self._deep_verify(
                        deps,
                        request,
                        answer,
                        canvases,
                        messages=list(state.values.get("messages") or []),
                    )
            except AwaitingReview:
                # Slot-free wait, woken by the child's terminal write. The
                # thread is finished, so the resumed segment re-enters this
                # post-loop without a model turn and finds the child again.
                context.park("waiting_for_children")
                return self._parked_result(request, deps)
            except AgentCancelled as exc:
                from inqtrix.agents.kernel.transcript import archive_checkpoint

                archive_checkpoint(deps, state, request, required=False)
                # Same contract as the model-boundary cancel in run():
                # spend is booked by execute_run_request, the run ends
                # CANCELLED (never a generic failure). A located finding
                # the revision had not decided yet is named ``cancelled``.
                _send_kernel_outcome(deps, final=cancel_final(exc), by_cancel=True)
                return AgentResult(
                    answer="",
                    result_type="agent_kernel_result",
                    raw={
                        "answer": "",
                        "usage": dict(deps.usage),
                        "result_state": {
                            "usage_total": deps.run_usage(),
                            "cancelled": True,
                            "cancel_reason": (
                                "token_budget_exceeded"
                                if isinstance(exc, AgentTokenBudgetExceeded)
                                else "client_requested_cancel"
                            ),
                            "execution": self._execution_payload(
                                request,
                                deps,
                                consent_reason=_kernel_consent_reason(deps),
                            ),
                        },
                    },
                )
        else:
            # A Stufe that verifies nothing is the published contract,
            # not a fallback — it says so instead of leaving the run's
            # review state to be guessed from an absence, in the activity
            # and in the outcome record. A request without a Stufe skips
            # the review by its legacy depth rule: no Stufe waived it, so
            # its record reads ``not_run`` (the mission records ``waived``
            # under the same condition).
            if deps.tier:
                deps.review_facts = {"last_review": {"status": "waived", "round": 0}}
            deps.emit(
                "inqtrix.agent.activity",
                waived_activity(
                    deps.verify_level,
                    tier=deps.tier,
                    language=_answer_language(deps),
                ),
            )
        answer = _validate_kernel_answer_citations(
            deps,
            answer,
            list(deps.evidence_refs.values()),
        )
        if "outputs" in deps.review_facts:
            # The record counts the statements of the answer as published.
            deps.review_facts["outputs"]["chat"] = answer
        answer_claim_bindings: list[dict[str, Any]] = []
        # Answer-artifact persistence belongs to the native RunService
        # publisher.  The kernel returns the post-verify Markdown and its
        # reference state; publication begins only after this algorithm has
        # completed, so no client can observe the final body before streaming.
        _stage_kernel_memory(deps, answer)
        from inqtrix.agents.kernel.transcript import archive_checkpoint

        archive_checkpoint(deps, state, request)
        # After everything that can still fail the run, before the
        # checkpoint goes: a failed delete must not lose a finished record.
        _send_kernel_outcome(deps, final="completed")
        self._checkpointer.delete_thread(run_id)
        deps.emit(
            PHASE_CHANGED_EVENT,
            {
                "phase": "done",
                "previous_phase": "execution",
                "snapshot": {
                    "current_node": _KERNEL_NODE,
                    "phase": "done",
                    "execution": self._execution_payload(
                        request, deps, consent_reason=_kernel_consent_reason(deps)
                    ),
                },
            },
        )
        # The surfaced reference list follows the answer's citations
        # (cited-only, basis fallback via _result_references).  The central
        # publisher persists this exact exported projection on the answer
        # artifact. all/allowed_citations stay the FULL ledger URLs: they are
        # source-ordering inputs, not the user-facing reference list.
        report_references = _result_references(
            answer,
            list(deps.evidence_refs.values()),
            basis_fallback=deps.gathered_sources,
        )
        return AgentResult(
            answer=answer,
            result_type="agent_kernel_result",
            raw={
                "answer": answer,
                "usage": dict(deps.usage),
                "result_state": {
                    "usage_total": deps.run_usage(),
                    "answer": answer,
                    "cancelled": False,
                    "report_references": report_references,
                    "answer_claim_bindings": answer_claim_bindings,
                    "all_citations": [
                        str(item.get("url") or "")
                        for item in deps.evidence_refs.values()
                        if item.get("url")
                    ],
                    "allowed_citations": [
                        str(item.get("url") or "")
                        for item in deps.evidence_refs.values()
                        if item.get("url")
                    ],
                    "execution": self._execution_payload(
                        request, deps, consent_reason=_kernel_consent_reason(deps)
                    ),
                },
            },
        )

    def _parked_result(
        self, request: "RunRequest", deps: KernelDeps
    ) -> "AgentResult":
        """The segment result of a run that parked (interrupt or child wait)."""
        from inqtrix.core.results import AgentResult

        return AgentResult(
            answer="",
            result_type="agent_kernel_parked",
            raw={
                "answer": "",
                "usage": dict(deps.usage),
                "result_state": {
                    "usage_total": deps.run_usage(),
                    "parked": True,
                    "cancelled": False,
                    "execution": self._execution_payload(
                        request,
                        deps,
                        consent_reason=_kernel_consent_reason(deps),
                    ),
                },
            },
        )

    def _park_for_limit(
        self,
        request: "RunRequest",
        context: "RunContext",
        deps: KernelDeps,
        *,
        gate: AgentLimitGate,
    ) -> "AgentResult":
        """Park one checkpoint-safe limit through the native input gate."""
        from inqtrix.core.results import AgentResult

        _log_disproportionate_budget(
            deps, run_kind="conversation", bound=gate.kind
        )
        record, created = create_or_get_limit_gate(
            self._control,
            run_id=deps.run_id,
            gate=gate,
            run_async=run_coro,
            language=resolve_answer_language(deps.question).code,
        )
        payload = gate.payload(clarification_id=record.clarification_id)
        deps.emit(LIMIT_REACHED_EVENT, {**payload, "state": "waiting_for_input"})
        if created:
            deps.emit(
                "inqtrix.agent.clarification.requested",
                {
                    "clarification_id": record.clarification_id,
                    "question": record.question,
                    "options": [dict(item) for item in record.options],
                    "question_count": 1,
                },
            )
            # The question is the MODEL's own words, put in front of the
            # reader verbatim, so it goes through the same guard the
            # canvas write uses.
            emit_narration(
                deps.emit,
                narration_id=f"n-limit-{record.clarification_id}",
                kind="limit",
                text=record.question,
                phase="execution",
                guard=deps.guard_reader_text,
            )
        context.park("waiting_for_input")
        return AgentResult(
            answer="",
            result_type="agent_kernel_parked",
            raw={
                "answer": "",
                "usage": dict(deps.usage),
                "result_state": {
                    "usage_total": deps.run_usage(),
                    "parked": True,
                    "cancelled": False,
                    "limit": payload,
                    "execution": self._execution_payload(
                        request,
                        deps,
                        consent_reason=_kernel_consent_reason(deps),
                    ),
                },
            },
        )

    def _apply_terminal_limit_choice(
        self,
        request: "RunRequest",
        deps: KernelDeps,
        *,
        gate: AgentLimitGate,
        choice: LimitChoice,
        clarification_id: str,
        transcript_state: Any,
    ) -> "AgentResult":
        """Apply an explicit partial/cancel choice exactly once on resume."""
        from inqtrix.core.results import AgentResult

        payload = {
            **gate.payload(clarification_id=clarification_id),
            "choice": choice,
            "state": "decided",
        }
        deps.emit(LIMIT_DECIDED_EVENT, payload)
        from inqtrix.agents.kernel.transcript import archive_checkpoint

        archive_checkpoint(deps, transcript_state, request)
        if choice == LIMIT_CHOICE_CANCEL:
            _send_kernel_outcome(deps, final="cancelled")
            self._checkpointer.delete_thread(deps.run_id)
            return AgentResult(
                answer="",
                result_type="agent_kernel_result",
                raw={
                    "answer": "",
                    "usage": dict(deps.usage),
                    "result_state": {
                        "usage_total": deps.run_usage(),
                        "cancelled": True,
                        "cancel_reason": "limit_cancelled_by_user",
                        "limit": payload,
                        "execution": self._execution_payload(
                            request,
                            deps,
                            consent_reason=_kernel_consent_reason(deps),
                        ),
                    },
                },
            )
        if choice != LIMIT_CHOICE_PARTIAL:
            raise RuntimeError(f'Unknown terminal limit decision: {choice!r}')

        answer = _partial_limit_answer(
            gate,
            list(deps.evidence_refs.values()),
            language=resolve_answer_language(deps.question).code,
        )
        answer = _validate_kernel_answer_citations(
            deps,
            answer,
            list(deps.evidence_refs.values()),
        )
        answer_claim_bindings: list[dict[str, Any]] = []
        deps.effective_response_form = "chat"
        # A partial answer ends the run as completed; no review ran on it.
        _send_kernel_outcome(deps, final="completed")
        self._checkpointer.delete_thread(deps.run_id)
        deps.emit(
            PHASE_CHANGED_EVENT,
            {
                "phase": "done",
                "previous_phase": "execution",
                "snapshot": {
                    "current_node": _KERNEL_NODE,
                    "phase": "done",
                    "execution": self._execution_payload(
                        request,
                        deps,
                        consent_reason=_kernel_consent_reason(deps),
                    ),
                    "limit": payload,
                },
            },
        )
        references = _result_references(
            answer,
            list(deps.evidence_refs.values()),
            basis_fallback=deps.gathered_sources,
        )
        return AgentResult(
            answer=answer,
            result_type="agent_kernel_result",
            raw={
                "answer": answer,
                "usage": dict(deps.usage),
                "result_state": {
                    "usage_total": deps.run_usage(),
                    "answer": answer,
                    "cancelled": False,
                    "partial": True,
                    "partial_reason": "limit_reached",
                    "limit": payload,
                    "report_references": references,
                    "answer_claim_bindings": answer_claim_bindings,
                    "all_citations": [
                        str(item.get("url") or "")
                        for item in deps.evidence_refs.values()
                        if item.get("url")
                    ],
                    "allowed_citations": [
                        str(item.get("url") or "")
                        for item in deps.evidence_refs.values()
                        if item.get("url")
                    ],
                    "execution": self._execution_payload(
                        request,
                        deps,
                        consent_reason=_kernel_consent_reason(deps),
                    ),
                },
            },
        )

    def _execution_payload(
        self,
        request: "RunRequest",
        deps: KernelDeps,
        *,
        consent_reason: str,
    ) -> dict[str, object]:
        """Canonical Agent Desk execution block for this kernel run."""
        from inqtrix.agents.source_policy import (
            execution_payload,
            knowledge_boundary_payload,
        )

        limits = _limit_payloads(deps)

        return execution_payload(
            effective_mode="agent_kernel",
            response_form=(
                deps.effective_response_form
                or request.response_form
                or "auto"
            ),
            depth=deps.depth,
            model=deps.model,
            reasoning_effort=deps.reasoning_effort,
            source_policy=deps.source_policy,
            consent_reason=consent_reason,
            tool_use_counts=deps.tool_use_counts,
            web_searches=deps.web_searches,
            limits=limits,
            tool_grants=deps.tool_grants,
            source_coverage=deps.source_coverage.entries(),
            work_profile=request.work_profile,
            **knowledge_boundary_payload(request.knowledge_filters),
        )

    def _role_result(
        self,
        request: "RunRequest",
        deps: KernelDeps,
        profile: WorkProfile,
        state: Any,
        *,
        status: str = "",
        detail: str = "",
    ) -> "AgentResult":
        """Finish one role run: a typed work result, never an answer.

        The result is read from the checkpoint, so a segment redelivered
        between the end of the loop and the stored result derives the same
        outcome again. A role stages no memory, archives no transcript (it
        holds nothing but re-read material) and runs no review of its own.
        """
        from inqtrix.core.results import AgentResult

        values = getattr(state, "values", None) or {}
        messages = list(values.get("messages") or [])
        structured = values.get("structured_response")
        if not status:
            status = (
                WORK_RESULT_COMPLETED if structured is not None else WORK_RESULT_MISSING
            )
        if status == WORK_RESULT_MISSING:
            log.warning(
                "Role run %s (%s) ended with free text instead of its typed result.",
                deps.run_id, profile.name,
            )
        deps.checkpointed_steps = _checkpointed_steps(state)
        deps.tool_calls_used = _checkpointed_tool_call_count(
            state, uncounted=_result_call_names(profile)
        )
        _log_disproportionate_budget(
            deps,
            run_kind=f"role {profile.name}",
            # A role never parks, so the only budget that can stop it is the
            # one its typed status names.
            bound=(
                "tool_calls" if status == WORK_RESULT_BUDGET_EXHAUSTED else ""
            ),
        )
        work_result = build_work_result(
            profile,
            status=status,
            structured=structured if status == WORK_RESULT_COMPLETED else None,
            messages=messages,
            detail=detail,
        )
        self._checkpointer.delete_thread(deps.run_id)
        execution = self._execution_payload(
            request, deps, consent_reason=_kernel_consent_reason(deps)
        )
        deps.emit(
            PHASE_CHANGED_EVENT,
            {
                "phase": "done",
                "previous_phase": "execution",
                "snapshot": {
                    "current_node": _KERNEL_NODE,
                    "phase": "done",
                    "execution": execution,
                },
            },
        )
        return AgentResult(
            answer="",
            result_type="agent_kernel_result",
            raw={
                "answer": "",
                "usage": dict(deps.usage),
                "result_state": {
                    "usage_total": deps.run_usage(),
                    "answer": "",
                    "cancelled": False,
                    "work_result": work_result,
                    "execution": execution,
                },
            },
        )

    def _current_run_canvases(self, deps: KernelDeps) -> list[Any]:
        """Load every document this run's work produced or updated.

        Its own canvases, plus the memo a delegated mission child wrote
        into the session: that memo is the deliverable of a Tief run, so
        the verification pass reviews and revises it instead of a second
        copy the kernel would otherwise write.
        """
        from inqtrix.pagination import decode_cursor

        def _list(run_id: str, kind: str) -> list[Any]:
            rows_out: list[Any] = []
            after: tuple[float, str] | None = None
            while True:
                rows, cursor = run_coro(
                    self._control.list_artifacts(
                        run_id, kind=kind, limit=50, after=after
                    )
                )
                for row in rows:
                    full, _revisions = run_coro(
                        self._control.get_artifact(run_id, row.artifact_id)
                    )
                    rows_out.append(full)
                if not cursor:
                    break
                after = decode_cursor(cursor)
            return rows_out

        canvases: list[Any] = _list(deps.run_id, "deliverable")
        seen = {item.artifact_id for item in canvases}
        child_runs = [
            str(child.get("run_id") or "")
            for child in deps.run_service.run_store.children(deps.run_id)
            if child.get("mode") == "workspace_agent"
            and child.get("status") == "completed"
        ]
        for run_id in (deps.run_id, *child_runs):
            for memo in _list(run_id, "memo"):
                if memo.artifact_id not in seen:
                    canvases.append(memo)
                    seen.add(memo.artifact_id)
        return canvases

    def _deep_verify(
        self,
        deps: KernelDeps,
        request: "RunRequest",
        answer: str,
        canvases: list[Any],
        *,
        messages: list[Any],
    ) -> str:
        """Review the full assignment and all outputs, then revise once."""
        from inqtrix.agents.control_ports import (
            ArtifactBatchRevision,
            ArtifactNotFound,
            ArtifactRevisionConflict,
        )
        from inqtrix.agents.evidence_context import reference_origins
        from inqtrix.agents.kernel.evidence_read import (
            build_review_evidence,
            own_reading_outputs,
            run_reading_diagnostics,
        )
        from inqtrix.agents.report_quality import cited_references
        from inqtrix.core.results import HandoverOutput, ReleasedSource

        _review_words = {"language": resolve_answer_language(deps.question).code}

        def _review_narration(text: str, *, final: bool) -> None:
            emit_narration(
                deps.emit,
                narration_id="kernel_deep_review",
                kind="synthesis",
                phase="execution",
                text=t(_review_words, "kernel_review_prefix", text=text),
                final=final,
                guard=deps.guard_reader_text,
            )

        def _review_summary() -> str:
            # The final narration after a verdict IS the outcome record's
            # summary, built from the same review facts the segment sends:
            # the narration cannot say more, or less, than the record.
            return build_outcome(
                {
                    **_kernel_review_report(deps.review_facts, by_cancel=False),
                    "language": _answer_language(deps),
                },
                run_id=deps.run_id,
                final="completed",
                guard=deps.guard_reader_text,
            )["summary"]

        from inqtrix.agents.kernel.middleware import (
            SKILL_INPUTS_RESOLVED_FLAG,
            SKILL_INPUTS_RESOLVED_MARKER,
        )

        resolved_skill_blocks: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            if not isinstance(content, str):
                continue
            message_type = type(message).__name__
            # Trust is anchored on SERVER-SET metadata —
            # the middleware's additional_kwargs flag or the load_skill
            # tool identity — never on user-forgeable content text. A
            # user question containing the literal marker is inert here.
            trusted_resolved_block = (
                message_type == "HumanMessage"
                and bool(
                    (getattr(message, "additional_kwargs", None) or {}).get(
                        SKILL_INPUTS_RESOLVED_FLAG
                    )
                )
            ) or (
                message_type == "ToolMessage"
                and getattr(message, "name", "") == "load_skill"
                and SKILL_INPUTS_RESOLVED_MARKER in content
            )
            if trusted_resolved_block:
                resolved_skill_blocks.append(content)
        from inqtrix.agents.source_policy import effective_source_policy

        effective_policy = effective_source_policy(deps.source_policy)
        policy_payload = {
            "source_policy": {
                "web": effective_policy.web,
                "knowledge": effective_policy.knowledge,
            },
        }
        effective_assignment = (
            f"{self._user_message(request, deps)}\n\n"
            "EFFECTIVE SERVER POLICY:\n"
            f"{json.dumps(policy_payload, ensure_ascii=False, sort_keys=True)}"
        )
        if resolved_skill_blocks:
            effective_assignment += (
                "\n\nRESOLVED SKILL INPUTS AND INSTRUCTIONS:\n"
                + "\n\n".join(resolved_skill_blocks)
            )
        with deps._evidence_lock:
            ledger = dict(deps.evidence_refs)
        allowed_references = {
            "chat": list(ledger.values()),
            **{item.artifact_id: [dict(ref) for ref in item.refs] for item in canvases},
        }
        outputs = {"chat": answer, **{item.artifact_id: item.content_markdown for item in canvases}}
        review_references = [
            ref for output_id, body in outputs.items()
            for ref in cited_references(body, allowed_references[output_id])
        ]
        mission_run_ids = list(dict.fromkeys(
            item.run_id for item in canvases if getattr(item, "run_id", None)
        ))

        answer_language = resolve_answer_language(deps.question).code or ""
        source_titles = {
            document_id: title
            for ref in ledger.values()
            if (document_id := str(ref.get("document_id") or ""))
            and (title := str(ref.get("title") or "").strip())
        }
        source_titles.update({
            item.artifact_id: str(item.title or "").strip() for item in canvases
        })
        # What this run read itself: a server record the review may rely on
        # for what the run's own outputs claim to have read.
        own_reading = run_reading_diagnostics(
            deps.run_id, deps.source_coverage.entries(),
            titles=source_titles, language=answer_language,
        )
        own_reading_ids = {item["basis_id"] for item in own_reading}

        def evidence_for(references: list[dict[str, Any]]) -> dict[str, Any]:
            built = build_review_evidence(
                deps, references,
                mission_run_ids=mission_run_ids,
                language=answer_language,
            )
            built["run_diagnostics"] = [*built["run_diagnostics"], *own_reading]
            return built

        evidence = evidence_for(review_references)
        known = {item.artifact_id: item for item in canvases}
        own_outputs = own_reading_outputs(allowed_references, known, run_id=deps.run_id)
        diagnostic_basis_ids = {}
        for output_id, references in allowed_references.items():
            origins = set(reference_origins(references))
            if output_id in known:
                origins.add(getattr(known[output_id], "run_id", ""))
            diagnostic_basis_ids[output_id] = {
                item["basis_id"] for item in evidence["run_diagnostics"]
                if item["basis_id"] not in own_reading_ids
                and (item.get("source_run_id") in origins or item.get("mission_run_id") in origins)
            } | (own_reading_ids if output_id in own_outputs else set())
        from inqtrix.agents.report_requirement import (
            reviews_rule_by_rule,
            rule_marker_names,
            rule_reader_names,
            rule_review_criteria,
        )

        # Each attached rule is one criterion of this review; the review
        # runs, so it is owed — the same predicate the run's
        # requirement.attached event answered with ``per_rule``.
        judged_rules = (
            tuple(deps.report_rules)
            if reviews_rule_by_rule(deps.report_rules, review_owed=True)
            else ()
        )
        subject = ReviewSubject(
            assignment=effective_assignment,
            outputs=outputs,
            canvases=tuple(
                HandoverOutput(
                    kind="canvas",
                    artifact_id=item.artifact_id,
                    revision=int(item.revision),
                    title=str(item.title or ""),
                )
                for item in canvases
            ),
            allowed_references=allowed_references,
            ledger=ledger,
            cited=review_references,
            evidence=evidence,
            evidence_for=evidence_for,
            diagnostic_ids=diagnostic_basis_ids,
            criteria=rule_review_criteria(judged_rules),
            criterion_readers=rule_reader_names(judged_rules, answer_language),
            marker_names=rule_marker_names(judged_rules, answer_language),
            # The run's clarifications as the store holds them, not as a
            # transcript phrased them: a resumed or compacted run reads the
            # same record.
            clarifications=run_clarifications(
                run_coro(deps.control.list_clarifications(deps.run_id))
            ),
            # What this run actually read is what the review may re-read.
            # The coverage ledger counts characters and knows no titles, so
            # the title comes from the run's own evidence ledger — the one
            # place a document id and its title already meet.
            released_sources=tuple(
                ReleasedSource(
                    kind=entry["kind"],
                    source_id=entry["source_id"],
                    version=str(entry["version"] or ""),
                    title=source_titles.get(entry["source_id"], ""),
                )
                for entry in deps.source_coverage.entries()
                if entry["kind"] in RELEASED_SOURCE_KINDS and entry["read_units"] > 0
            ),
            user_text=deps.user_text(),
            evidence_run_id=deps.run_id,
            language=answer_language,
        )
        host = ReviewHost(
            scope=kernel_child_scope(deps),
            emit=deps.emit,
            visible_to=deps.visible_to,
            llm=deps.llm,
            timeout=deps.timeout,
            platform=deps.platform,
            revision_call=ModelCall(_KERNEL_NODE, deps.model, deps.reasoning_effort),
            review_call=_review_model_call(deps),
            role_available=deps.run_service is not None,
        )
        deps.check_abort()
        # The Stufe decides WHETHER a run is reviewed; the size of the
        # material decides HOW. A bounded role that re-reads the released
        # documents for itself is the right instrument above the budget
        # and the most expensive one below it.
        review = request_review(
            host,
            subject,
            on_start=lambda: _review_narration(
                t(_review_words, "kernel_review_running"), final=False
            ),
        )
        if review.usage:
            # The in-prompt transport spends on THIS run's model; only the
            # role carried its own booking through the child run.
            deps.book_usage(
                review.usage["prompt_tokens"], review.usage["completion_tokens"]
            )
        if review.verdict is None:
            # What the outcome record reports; the mission's words for a
            # review that was owed and produced no usable verdict.
            deps.review_facts = {
                "last_review": {"status": "unavailable", "round": 1, "reason": review.reason},
            }
            deps.emit(
                "inqtrix.agent.activity",
                unavailable_activity(review, _review_words["language"] or ""),
            )
            log.warning(
                "Run %s: the '%s' report-quality level owes a review pass; it "
                "did not run (transport=%s, reason=%s).",
                deps.run_id,
                deps.verify_level,
                review.transport or "none",
                review.reason,
            )
            # The reason's own sentence; the raw detail went to the log with
            # the activity's reader detail.
            _review_narration(
                t(_review_words, f"kernel_review_{review.reason}"), final=True
            )
            return answer
        from inqtrix.observability.trace_scores import record_deep_review_scores

        record_deep_review_scores(review.verdict)
        assessment = assess_review(review, subject)
        located = assessment.located
        language = _review_words["language"] or ""
        # A verdict that names nothing is no usable review, as for a
        # mission; any other verdict is reported with the findings settled
        # so far, and a located one the revision never decides is named by
        # the exit that stopped it (``_kernel_review_report``).
        deps.review_facts = (
            {"last_review": {"status": "unavailable", "round": 1, "reason": "inconsistent"}}
            if assessment.status == "inconsistent"
            else {
                "last_review": {"status": "completed", "round": 1},
                "assessment": assessment,
                "results": list(assessment.results),
                "language": language,
                # The texts the segment delivers: the reviewed ones until a
                # written revision or the final citation check replaces them.
                "outputs": dict(outputs),
            }
        )

        def name_unapplied(results: list[Any]) -> None:
            # The findings the outputs did not take over, each with its
            # reason in reader words: what the narration counts as open.
            unapplied = unapplied_activity(results, assessment.names, language)
            if unapplied is not None:
                deps.emit("inqtrix.agent.activity", unapplied)

        if assessment.status != "revise":
            if marked_results(assessment.results):
                # Marked passages are the review's substance too, though no
                # revision is asked for them.
                deps.emit(
                    "inqtrix.agent.activity",
                    findings_activity(
                        assessment, language,
                        titles={item.artifact_id: item.title for item in canvases},
                    ),
                )
            name_unapplied(assessment.results)
            _review_narration(_review_summary(), final=True)
            return answer
        deps.emit(
            "inqtrix.agent.activity",
            findings_activity(
                assessment, language,
                titles={item.artifact_id: item.title for item in canvases},
            ),
        )
        _review_narration(
            t_count(_review_words, "kernel_review_findings_revising", len(located)),
            final=False,
        )
        deps.check_abort()
        revision = revise_outputs(
            host,
            subject,
            assessment,
            output_bundle=json.dumps(
                {
                    "chat": answer,
                    "artifacts": [
                        {
                            "artifact_id": item.artifact_id,
                            "revision": item.revision,
                            "content_markdown": item.content_markdown,
                            "refs": [dict(ref) for ref in item.refs],
                        }
                        for item in canvases
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if revision.usage:
            deps.book_usage(
                revision.usage["prompt_tokens"], revision.usage["completion_tokens"]
            )
        deps.review_facts["results"] = [*assessment.results, *revision.results]
        if revision.status != "revised":
            name_unapplied([*assessment.results, *revision.results])
            _review_narration(_review_summary(), final=True)
            return answer
        # A revision is writer text like the output it revises: grouped
        # citations become single ones before the text is stored or read.
        revised_outputs = {
            output_id: normalize_writer_markdown(text)
            for output_id, text in revision.outputs.items()
        }
        revised = revised_outputs["chat"]
        results = [*assessment.results, *revision.results]
        applied = [result for result in results if result.state == "applied"]
        # Only a canvas with an applied edit is written: an unchanged one
        # would become a new revision with a chip for nothing.
        targeted_ids = {
            result.output_id for result in applied if result.output_id != "chat"
        }
        try:
            revised_records = run_coro(
                self._control.revise_session_artifacts_atomically(
                    run_id=deps.run_id,
                    session_id=deps.session_id or None,
                    revisions=[
                        ArtifactBatchRevision(
                            artifact_id=artifact_id,
                            expected_revision=known[artifact_id].revision,
                            content_markdown=revised_outputs[artifact_id],
                        )
                        for artifact_id in sorted(targeted_ids)
                    ],
                )
            )
        except (ArtifactNotFound, ArtifactRevisionConflict) as exc:
            # The whole revision stays discarded, chat included: every
            # applied finding is named after all.
            results = [
                result.named("write_conflict") if result.state == "applied" else result
                for result in results
            ]
            deps.review_facts["results"] = results
            log.warning(
                "Deep revision CAS conflict (error_type=%s); findings not applied: %s.",
                type(exc).__name__,
                ", ".join(f"{result.finding_id}={result.reason}" for result in results),
            )
            name_unapplied(results)
            _review_narration(_review_summary(), final=True)
            return answer
        except Exception as exc:  # noqa: BLE001 — preserve reviewed outputs
            results = [
                result.named("store_error") if result.state == "applied" else result
                for result in results
            ]
            deps.review_facts["results"] = results
            log.warning(
                "Deep revision store error (error_type=%s); findings not applied: %s.",
                type(exc).__name__,
                ", ".join(f"{result.finding_id}={result.reason}" for result in results),
            )
            name_unapplied(results)
            _review_narration(_review_summary(), final=True)
            return answer
        # This batch is otherwise a silent artifact write path — without
        # these events the deep-mode revision produces neither a file
        # chip nor a live canvas refresh.
        for stored in revised_records:
            deps.emit(ARTIFACT_UPDATED_EVENT, artifact_event_payload(stored))
        deps.review_facts["outputs"] = _written_outputs(
            deps.review_facts["outputs"], revised_outputs, targeted_ids
        )
        name_unapplied(results)
        _review_narration(_review_summary(), final=True)
        return revised

    # -- segment assembly ---------------------------------------------------#

    def _build_deps(
        self,
        run_id: str,
        llm: Any,
        context: "RunContext",
        request: "RunRequest",
        *,
        pin_tier: str = "",
        pin_effort: str = "",
        depth: str = "normal",
        node: str = _KERNEL_NODE,
    ) -> KernelDeps:
        settings = context.agent_settings
        provider_models = getattr(llm, "models", None)
        requested_tier = (getattr(settings, "model_tier", "") or "").strip()
        requested_model = (getattr(settings, "model", "") or "").strip()
        requested_effort = (getattr(settings, "effort", "") or "").strip()
        if (pin_tier and not requested_tier) or (
            pin_effort and not requested_effort
        ):
            # Precedence: explicit user override > skill pin > depth
            # > tier map. The pin only fills what the request left empty.
            log.info(
                "Skill pin active: model_tier=%r effort=%r",
                pin_tier or "(none)",
                pin_effort or "(none)",
            )
        requested_tier = requested_tier or pin_tier
        requested_effort = requested_effort or pin_effort
        model_overrides = {
            key: value for key, value in (
                ("model_tier", requested_tier),
                ("model", requested_model),
                ("effort", requested_effort),
            ) if value
        }
        if depth == "deep" and not requested_effort and node == _KERNEL_NODE:
            # Deep mode: the kernel node runs on high
            # reasoning effort unless the request or a skill pin chose
            # one — the tier stays with the tier map (agent_kernel is
            # high-tier already). It stays local to the reasoning loop:
            # a role resolves under its OWN routing node, whose effort the
            # operator owns and the user selects, so elevating it here
            # would let the parent's depth overrule both.
            requested_effort = "high"
            log.info("Deep mode: reasoning_effort=high for the kernel.")
        if provider_models is None and not requested_model:
            desc = describe_unresolved_resolution(node, requested_tier)
        else:
            desc = describe_resolution(
                node,
                provider_models,
                requested_tier,
                requested_model=requested_model,
                requested_effort=requested_effort,
            )
        deps = KernelDeps(
            run_id=run_id,
            control=self._control,
            platform=self._platform,
            llm=llm,
            model=desc.get("model") or None,
            reasoning_effort=desc.get("effort") or None,
            model_overrides=model_overrides,
            timeout=float(getattr(settings, "reasoning_timeout", REASONING_TIMEOUT)),
            event_sink=context.event_sink,
            capability_registry=self._capabilities,
            capability_context=self._capability_context(context, request),
            cancel_token=context.cancel_token,
            token_budget=int(context.token_budget or 0),
            # The run's explicit editor target (empty for most runs) —
            # the editor tools refuse every other document.
            target_document_id=request.document_id or "",
            # The attachment is the consent — read_research_report
            # refuses every id outside this tuple, so the model cannot
            # read an arbitrary run it merely has visibility for.
            attached_report_ids=tuple(
                str(report.get("report_id") or "")
                for report in (request.attached_reports or ())
                if str(report.get("report_id") or "")
            ),
            # A read of one names the report by the title the user saw.
            attached_report_titles={
                str(report.get("report_id")): str(report.get("title"))
                for report in (request.attached_reports or ())
                if report.get("report_id") and report.get("title")
            },
        )
        # Context management is resolved per run (the compiled graph is
        # shared): the compaction trigger follows the RESOLVED model's
        # card, the offload threshold is a platform constant.
        deps.context_trigger_tokens = resolve_context_trigger_tokens(
            self._platform, deps.model
        )
        deps.context_offload_chars = int(
            getattr(self._platform, "kernel_context_offload_chars", 0) or 0
        )
        from inqtrix.model_cards import resolve_model_card
        from inqtrix.agents.context_sources import has_external_input

        card = resolve_model_card(deps.model) if deps.model else None
        if card is not None and card.context_window_tokens:
            deps.context_input_budget = max(
                1, card.context_window_tokens - card.max_output_tokens
                - max(1024, card.context_window_tokens // 50),
            )
        deps.source_service = self._sources
        deps.context_sources_available = self._sources is not None
        # Only raw file/image inputs keep the conservative transcript lock;
        # an editing target that the run actually reads becomes a named
        # material dependency of the transcript instead.
        deps.context_has_external_inputs = bool(has_external_input(request.messages))
        deps.emit("inqtrix.node.model_resolution", desc)
        return deps

    def _load_request_skills(
        self, request: "RunRequest", context: "RunContext"
    ) -> list[Any]:
        """Load the router-admitted attached skills (loud, never partial).

        Same trust model as the phase machine: the router admitted the
        ids with the caller's grants, the segment executes what was
        admitted (``get_admitted``). Loading is separate from
        activation so the skill model pins can inform the deps
        BEFORE the model resolution.
        """
        if not request.skill_ids:
            return []
        if self._skill_service is None:
            raise RuntimeError(
                "Skills are attached but not configured on this server."
            )
        records: list[Any] = []
        for skill_id in request.skill_ids:
            try:
                visible_to = getattr(
                    self._capability_context(context, request), "visible_to", None
                )
                record, _access = run_coro(
                    self._skill_service.get_visible(
                        skill_id,
                        tenant_id=(
                            context.principal.tenant_id
                            if context.principal is not None
                            else "default"
                        ),
                        visible_to=visible_to,
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
            records.append(record)
        return records

    def _reactivate_loaded_skills(
        self, deps: KernelDeps, snapshot: Any
    ) -> None:
        """Re-arm restrictions from load_skill markers in the transcript.

        A skill loaded BEFORE a park narrowed the tool surface; the
        resume segment must not forget that (a forgotten allowlist
        would silently widen what the model may call).
        """
        from inqtrix.agents.kernel.tools import SKILL_LOADED_MARKER

        if snapshot is None or not snapshot.values:
            return
        prefix = SKILL_LOADED_MARKER.split("{", 1)[0]
        for message in snapshot.values.get("messages") or []:
            if getattr(message, "type", "") != "tool":
                continue
            # Trust the PRODUCING TOOL, not the content prefix: other
            # tools relay provider-controlled text verbatim (web
            # answers), so a marker-shaped result from them must never
            # activate a skill.
            if getattr(message, "name", "") != "load_skill":
                continue
            content = str(getattr(message, "content", "") or "")
            if not content.startswith(prefix):
                continue
            marker_body = content[len(prefix):].split("]", 1)[0]
            skill_id, separator, admitted_revision = marker_body.rpartition("@")
            if not skill_id or not separator or not admitted_revision:
                log.warning("Loaded skill has no revision marker in the checkpoint.")
                raise RuntimeError(
                    ("The loaded skill has no reliable revision marker; resume is "
                    "aborted.")
                )
            if self._skill_service is None:
                log.warning(
                    "Loaded skill %s cannot be reactivated without the skill service.",
                    skill_id,
                )
                raise RuntimeError(
                    f'Loaded skill {skill_id} cannot be safely reactivated without the skill service.'
                )
            try:
                record, _access = run_coro(
                    self._skill_service.get_visible(
                        skill_id,
                        tenant_id=(
                            deps.principal.tenant_id
                            if deps.principal is not None
                            else "default"
                        ),
                        visible_to=getattr(
                            deps.capability_context, "visible_to", None
                        ),
                    )
                )
            except Exception:  # noqa: BLE001 — visible via the warning
                log.warning(
                    "Loaded skill %s is no longer available on resume; resume is "
                    "aborted (fail-closed).",
                    skill_id,
                )
                raise RuntimeError(
                    f'Loaded skill {skill_id} cannot be safely reactivated on resume.'
                )
            if str(record.revision) != admitted_revision:
                log.warning(
                    "Loaded skill %s changed revision (%s -> %s).",
                    skill_id,
                    admitted_revision,
                    record.revision,
                )
                raise RuntimeError(
                    f'Loaded skill {skill_id} changed since activation; resume is aborted.'
                )
            deps.activate_skill(record)

    def _skills_context(self, deps: KernelDeps) -> str:
        """Attached-skill instructions + the model-facing disclosure list.

        The disclosure block names ONLY the caller-visible
        ``model_allowed`` autocomplete skills under a deterministic
        character budget; overflow is a VISIBLE "... und N weitere"
        line, never a silent cut. Shared-in skills never appear (their
        grants are not resolvable here, and recipients are structurally
        ``user_only`` anyway).
        """
        parts: list[str] = []
        if deps.skill_service is not None and deps.principal is not None:
            try:
                visible = run_coro(
                    deps.skill_service.list_visible(
                        tenant_id=deps.principal.tenant_id,
                        visible_to=getattr(
                            deps.capability_context, "visible_to", None
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 — a lookup outage costs the list
                log.warning(
                    "Skill disclosure unavailable (error_type=%s).",
                    type(exc).__name__,
                )
                visible = []
            activated = {skill.id for skill in deps.skills}
            candidates = [
                record
                for record, _shared in visible
                if record.invocation == "model_allowed"
                and record.include_in_autocomplete
                and record.id not in activated
            ]
            budget = self._platform.skills_disclosure_budget_chars
            lines: list[str] = []
            used = 0
            dropped = 0
            for record in candidates:
                line = (
                    f"- /{record.label} ({record.id}): "
                    f"{record.description or record.title}"
                    + (
                        f" — Wann: {record.when_to_use}"
                        if record.when_to_use
                        else ""
                    )
                )
                if used + len(line) > budget:
                    dropped += 1
                    continue
                lines.append(line)
                used += len(line)
            if dropped:
                lines.append(f"- ... and {dropped} more")
            if lines:
                parts.append(
                    "Available skills (activate with load_skill when needed):\n"
                    + "\n".join(lines)
                )
        return "\n\n".join(parts)

    def _capability_context(
        self, context: "RunContext", request: "RunRequest"
    ) -> Any:
        """Per-segment tool identity and server-pinned knowledge boundary.

        Resolved once per segment like the workspace `_RunDeps`;
        ``visible_to=None`` stays the historical see-everything view of
        the anonymous/static modes only.
        """
        from inqtrix.capabilities import CapabilityContext

        visible_to = None
        if self._permissions is not None and context.principal is not None:
            visible_to = run_coro(
                self._permissions.resolve_user_context(context.principal)
            )
        knowledge_collection_ids = pinned_knowledge_collection_ids(
            request.knowledge_filters,
            scoped_principal=bool(
                context.principal is not None
                and context.principal.user_id is not None
            ),
        )
        return CapabilityContext(
            principal=context.principal,
            visible_to=visible_to,
            workspace_id=context.workspace_id,
            run_id=context.run_id,
            question=request.question,
            knowledge_collection_ids=knowledge_collection_ids,
            target_document_id=request.document_id or None,
            search_provider=context.providers.search,
            authority_check=context.authority_check,
            on_provider_retry=(
                lambda notice: context.event_sink(
                    "inqtrix.agent.activity",
                    provider_retry_activity(
                        notice,
                        language=resolve_answer_language(
                            request.question
                        ).code,
                    ),
                )
                if context.event_sink is not None
                else None
            ),
        )

    def _graph_input(
        self,
        request: "RunRequest",
        run_id: str,
        existing: Any,
        deps: KernelDeps,
    ) -> Any:
        if existing is None or not existing.values:
            if self._run_service is None:
                raise RuntimeError(
                    ("Checkpoint status cannot be safely checked without the run store; "
                    "the run will not be restarted.")
                )
            ensure_checkpoint_restart_safe(
                run_id,
                control=self._control,
                run_store=self._run_service.run_store,
                run_async=run_coro,
            )
            if request.handover is not None:
                # A role is isolated: its whole context is the typed
                # handover — no session history, no memory, no catalog.
                return {
                    "messages": [
                        {"role": "user", "content": render_handover(request.handover)}
                    ]
                }
            # This branch runs exactly ONCE per run (no checkpoint
            # yet) — the durable record of which comments rode along.
            # The comment text travels in full (no sanitizer cap on the
            # key); the quote is a VISIBLY shortened preview, its full
            # text stays reachable via the document itself.
            if request.canvas_context is not None:
                context = request.canvas_context
                deps.emit(
                    CANVAS_CONTEXT_ATTACHED_EVENT,
                    {
                        "artifact_id": context.artifact_id,
                        "revision": context.revision,
                        "comments": [
                            {
                                "comment": comment.comment,
                                "quote_preview": (
                                    comment.quote
                                    if len(comment.quote) <= 120
                                    else f"{comment.quote[:120]}…"
                                ),
                            }
                            for comment in context.comments
                        ],
                    },
                )
            if request.report_requirement:
                from inqtrix.agents.report_requirement import (
                    requirement_attached_payload,
                )

                deps.emit(
                    REQUIREMENT_ATTACHED_EVENT,
                    requirement_attached_payload(
                        request.report_requirement,
                        request.report_rules,
                        review_owed=review_required(deps.verify_level),
                        run_id=run_id,
                        # The kernel's review judges each attached rule.
                        rule_by_rule=True,
                    ),
                )
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": self._user_message(request, deps),
                    }
                ]
            }
        if existing.tasks and any(
            task.interrupts for task in existing.tasks
        ):
            from langgraph.types import Command

            return Command(resume=self._resume_value(run_id, existing))
        # Crash between segments without a pending interrupt: the
        # checkpoint fast-forwards past completed nodes on its own.
        return None

    def _user_message(
        self, request: "RunRequest", deps: KernelDeps
    ) -> str:
        """The per-run user message: session context + assignment.

        Context rides the user message because the compiled graph (and
        its system prompt) is shared across runs. An explicit
        ``request.history`` (API callers) wins over the builder, same
        precedence as the phase machine.
        """
        from inqtrix.agents.prompts import (
            build_canvas_context_section,
            build_kernel_user_message,
        )

        self._hydrate_session_context(request, deps)
        self._hydrate_collection_catalog(deps)
        history_block = deps.session_history
        registry = deps.artifact_registry
        last_form = deps.last_response_form
        prior_evidence_count = deps.prior_evidence_count
        from inqtrix.agents.skills_runtime import build_tool_directives_line

        answer_language = resolve_answer_language(deps.question)
        log.info(
            "answer language resolved: %s (source=%s)",
            answer_language.code or "unresolved",
            answer_language.source,
        )
        message = build_kernel_user_message(
            deps.question,
            answer_language=answer_language.code,
            history_block=history_block,
            artifact_registry=registry,
            last_response_form=last_form,
            prior_evidence_count=prior_evidence_count,
            memory_briefing=deps.memory_briefing,
            response_form=request.response_form or "",
            autonomy=deps.autonomy,
            depth=deps.depth,
            tier=deps.tier,
            skills_block=self._skills_context(deps),
            tool_directives_line=build_tool_directives_line(
                request.tool_directives or ()
            ),
            canvas_context_section=(
                build_canvas_context_section(request.canvas_context)
                if request.canvas_context is not None
                else ""
            ),
            target_document_id=request.document_id or "",
            report_requirement=request.report_requirement or "",
            attached_reports=list(request.attached_reports or ()),
            collection_catalog=deps.collection_catalog,
            context_sources_available=deps.context_sources_available,
        )
        return message

    def _hydrate_session_context(
        self, request: "RunRequest", deps: KernelDeps
    ) -> None:
        """Load the session context once for every kernel route.

        Conversational history, artifact registry, last effective
        response form and prior evidence count — the quick web route
        included.
        """
        from inqtrix.agents.session_context import build_session_context

        history_block = request.history or ""
        registry: tuple[dict[str, Any], ...] = deps.artifact_registry
        last_form = deps.last_response_form
        prior_evidence_count = deps.prior_evidence_count
        if deps.session_id and self._run_service is not None:
            pack = build_session_context(
                deps.session_id,
                run_store=self._run_service.run_store,
                control=self._control,
                run_async=run_coro,
                visible_to=getattr(
                    deps.capability_context, "visible_to", None
                ),
                current_run_id=deps.run_id,
            )
            if not history_block:
                history_block = pack.history_block
            registry = pack.artifact_registry
            last_form = pack.last_response_form
            prior_evidence_count = pack.prior_evidence_count
            if pack.prior_references:
                # The session's retained sources are known evidence for
                # this turn: a follow-up that cites "[W3]" from the
                # previous answer keeps the citation (their labels are
                # reserved first, so new sources number after them)
                # instead of seeing it marked unsupported — and it need
                # not search again for what the session already holds.
                # Idempotent on resume: the merge is by reference id.
                deps.register_references(list(pack.prior_references))
        deps.session_history = history_block
        deps.artifact_registry = registry
        deps.last_response_form = last_form
        deps.prior_evidence_count = prior_evidence_count
        self._recall_memory_briefing(request, deps)

    def _hydrate_collection_catalog(self, deps: KernelDeps) -> None:
        """List the run's knowledge collections for the model.

        The kernel had no catalog at all: it searched blind, and its
        tool docstring even promised a project-wide sweep that the run's
        pinned boundary contradicts. The mission engine has listed its
        collections since day one — this puts the kernel on the same
        footing. Latched like the memory briefing: the deep-verify
        rebuild calls the message builder a second time.

        ``None`` when the catalog cannot be read, so the block is
        OMITTED rather than asserting an empty knowledge base.
        """
        if deps.collection_catalog_loaded:
            return
        deps.collection_catalog_loaded = True
        if deps.source_policy.knowledge != "available":
            deps.collection_catalog = ()
            return
        if deps.capability_registry is None:
            deps.collection_catalog = None
            return
        try:
            output = run_coro(
                deps.capability_registry.invoke(
                    "knowledge.collections.list",
                    {},
                    deps.capability_context,
                )
            )
        except Exception:  # noqa: BLE001 - degrade VISIBLY, never silently
            log.warning(
                "Knowledge collection catalog unavailable for the kernel; the run "
                "starts without a collection overview.",
                exc_info=True,
            )
            deps.collection_catalog = None
            return
        deps.collection_catalog = tuple(
            {
                "collection_id": str(getattr(item, "id", "")),
                "name": str(getattr(item, "name", "")),
                "document_count": int(getattr(item, "document_count", 0) or 0),
            }
            for item in getattr(output, "collections", [])
        )

    def _recall_memory_briefing(
        self, request: "RunRequest", deps: KernelDeps
    ) -> None:
        """Load the non-evidentiary memory briefing (mission parity).

        Same gates as ``_load_memory_briefing`` in the mission engine:
        service wired, principal present, per-user opt-in (privacy
        default OFF), provider available and eligible. Every outcome is
        visible — ``deps.memory_status`` mirrors the mission vocabulary
        and a recall failure narrates instead of silently disabling.
        """
        if deps.memory_recalled:
            # Once per segment: _hydrate_session_context runs again for
            # the deep-verify assignment rebuild — a second recall would
            # double the briefing round-trip for every deep run.
            return
        deps.memory_recalled = True
        service = deps.memory
        principal = deps.principal
        if service is None or principal is None or not deps.memory_opt_in:
            deps.memory_status = "disabled"
            return
        status = service.status(principal)
        if not status.get("available") or not status.get(
            "principal_eligible"
        ):
            deps.memory_status = "disabled"
            return
        from inqtrix.agents.memory_ports import AgentMemoryUnavailable

        try:
            briefing, memory_status = run_coro(
                service.recall_briefing(
                    principal=principal,
                    query=deps.question or "",
                    limit=5,
                )
            )
        except AgentMemoryUnavailable:
            briefing, memory_status = "", "unavailable"
        deps.memory_briefing = briefing
        deps.memory_status = memory_status
        if memory_status == "used":
            log.info("Kernel memory briefing loaded (not citable).")
        elif memory_status == "unavailable":
            log.warning(
                "Kernel memory unavailable; continuing without long-term context."
            )
            emit_narration(
                deps.emit,
                narration_id="kernel_memory_recall",
                kind="status",
                phase="intake",
                text=t(
                    {"language": resolve_answer_language(deps.question).code},
                    "agent_memory_detail_unavailable",
                ),
                guard=deps.guard_reader_text,
            )

    def _resume_value(self, run_id: str, snapshot: Any) -> Any:
        """Resolve the resume payload from the control store."""
        raw: Any = None
        interrupt_id = ""
        for task in snapshot.tasks:
            for intr in task.interrupts:
                raw = intr.value
                interrupt_id = str(intr.id)
                break
        origin, payload = translate_kernel_interrupt(raw)
        if origin == CLARIFICATION_INTERRUPT:
            record = run_coro(
                self._control.get_clarification(run_id, payload["id"])
            )
            return {
                "kind": "clarification",
                "status": record.status,
                "answer": record.answer,
                "option_id": record.option_id,
                "answers": dict(record.answers),
            }
        if origin == CHILDREN_INTERRUPT:
            # No row backs a children wait: the tool re-reads its
            # child's terminal outcome from the run store itself.
            return {"kind": "children"}
        approval = run_coro(
            self._control.get_approval(
                run_id, _tool_approval_id(run_id, interrupt_id)
            )
        )
        if approval.status == "pending":
            # The worker only wakes on a decision; a pending row here
            # means the wake path broke — resuming blind would grant
            # something no one approved.
            raise RuntimeError(
                ("Tool approval is still pending; the run cannot resume without a "
                "decision.")
            )
        return {
            "decisions": _hitl_decisions(
                approval, action_count=len(payload["action_requests"])
            )
        }

    def _ensure_tool_approval(
        self,
        run_id: str,
        deps: KernelDeps,
        *,
        interrupt_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Idempotently create the ``kind="tool"`` approval row.

        The approval id derives from the langgraph interrupt id, which
        is STABLE across re-entry and agent rebuilds (frozen by the
        harness contract tests) — a crash between park and decision
        re-enters here and finds the existing row instead of asking
        twice.
        """
        approval_id = _tool_approval_id(run_id, interrupt_id)
        try:
            run_coro(self._control.get_approval(run_id, approval_id))
            return
        except ApprovalNotFound:
            pass
        actions = [
            {
                "tool": str(request.get("name", "")),
                # The args ARE the approval content (a web query stands
                # here verbatim).
                "args": dict(request.get("args") or {}),
                "summary": str(request.get("description", "")),
            }
            for request in payload["action_requests"]
        ]
        # A knowledge gate says WHICH collections the run may
        # reach. The boundary is pinned at submission and enforced
        # server-side, so the names are known here — asking about "the
        # project's internal collections" without naming them made the
        # decision blind. Sorted for replay-stable payload identity.
        scope_names = _knowledge_scope_names(deps, actions)
        gate_payload: dict[str, Any] = {"actions": actions}
        if scope_names is not None:
            gate_payload["knowledge_scope"] = scope_names
        record = run_coro(
            self._control.create_approval(
                ApprovalRecord(
                    approval_id=approval_id,
                    run_id=run_id,
                    kind="tool",
                    payload=gate_payload,
                    interrupt_key=interrupt_id,
                )
            )
        )
        deps.emit(
            "inqtrix.agent.approval.requested",
            {
                "approval_id": record.approval_id,
                "kind": "tool",
                **gate_payload,
            },
        )

    # -- graph --------------------------------------------------------------#

    def _compiled_graph(
        self,
        autonomy: str,
        *,
        source_policy: Any,
        max_tool_calls: int,
        profile: WorkProfile | None = None,
    ) -> Any:
        """The compiled kernel graph of one policy variant (cached).

        Stateless and shared across runs: the chat model delegates to
        the deps ContextVar, so no per-run state is baked in — only the
        policy's ``interrupt_on`` and enforced source tool surface differ per
        variant (both are compile-time). A work profile is such a variant:
        its tools are the intersection of the profile and the source policy,
        it gates nothing (a role re-reads what its parent already reached
        and never asks the user) and it ends with its typed result.
        """
        key = (
            autonomy if profile is None else "",
            source_policy.web,
            source_policy.knowledge,
            max_tool_calls,
            profile.name if profile is not None else "",
        )
        with self._graph_lock:
            if key not in self._graphs:
                from inqtrix.agents.harness import build_kernel_agent

                from inqtrix.agents.prompts import (
                    build_agent_kernel_system_prompt,
                    build_work_profile_system_prompt,
                )
                from inqtrix.agents.source_policy import kernel_tool_allowed

                tools = [
                    tool
                    for tool in build_kernel_tools()
                    if kernel_tool_allowed(
                        str(getattr(tool, "name", "")),
                        policy=source_policy,
                    )
                    and (
                        profile is None
                        or str(getattr(tool, "name", "")) in profile.allowed_tools
                    )
                ]
                response_format = None
                if profile is not None:
                    from langchain.agents.structured_output import ToolStrategy

                    # The framework would retry a schema violation without
                    # bound; the kernel's bounded recovery owns the repair.
                    response_format = ToolStrategy(
                        profile.result_model, handle_errors=False
                    )

                self._graphs[key] = build_kernel_agent(
                    build_tool_chat_model(
                        _DepsChatProvider(),
                        # THE one usage-booking channel: per generation
                        # into the current segment's accumulator.
                        usage_hook=lambda prompt, completion: kernel_deps()
                        .book_usage(prompt, completion),
                    ),
                    tools=tools,
                    system_prompt=(
                        build_agent_kernel_system_prompt()
                        if profile is None
                        else build_work_profile_system_prompt(profile.role_prompt)
                    ),
                    interrupt_on=(
                        interrupt_config_for(autonomy) if profile is None else None
                    ),
                    checkpointer=self._checkpointer.saver(),
                    max_tool_calls=max_tool_calls,
                    response_format=response_format,
                    # Summarization is graph-compile-time; its TRIGGER is
                    # per-run via the deps ContextVar, so the cache key
                    # stays model-independent.
                    context_keep_messages=(
                        self._platform.kernel_context_keep_messages
                    ),
                )
            return self._graphs[key]


def _final_answer(messages: list[Any]) -> str:
    """The last AI message text of a finished kernel loop."""
    for message in reversed(messages):
        if getattr(message, "type", "") == "ai":
            if getattr(message, "invalid_tool_calls", None):
                continue
            content = getattr(message, "content", "")
            if isinstance(content, str) and content.strip():
                return normalize_writer_markdown(content)
    return ""


def _result_call_names(profile: WorkProfile | None) -> frozenset[str]:
    """Call names under which a role delivers its typed result (no tool runs)."""
    if profile is None:
        return frozenset()
    return frozenset({profile.result_model.__name__})


def role_tool_budget(
    platform: Any, *, depth: str, tier: str, handover: "WorkHandover"
) -> int:
    """THE tool budget of one role run, derived from the size of its subject.

    A role that verifies a run re-reads that run's material, so its work is
    linear in how much material there is — and a constant cannot be linear
    in anything. A measured review role was cut at 110% of a 30-call ceiling
    while its step and token budgets sat near 20%: the ceiling had stopped
    measuring the cost it stands for, and the run that ordered a thorough
    answer got a verification that ran out of room.

    Two bounds keep the derivation honest in both directions. The FLOOR is
    the operator's normal allowance, so no role gets less than a run
    without a derived budget. The CEILING is how far the tier
    that commissioned the role may REACH — its extension ceiling, not its
    base allowance.

    That second choice resolves a real tension in the budget rule, and it is
    written here rather than left to the arithmetic. "Never richer than the
    run that ordered it" read as the base allowance makes floor and ceiling
    coincide for every tier whose base IS the normal allowance — which is
    every tier except the deep one. The derivation would then be inert for
    ``gruendlich``, the default tier, which owes a verification like any
    other: a role would stay on the flat allowance on the path most runs
    take. The extension ceiling is what the operator already declared as the
    most one run of that tier may ever spend; a conversation reaches it by
    asking its user, and a role — which has no user to ask — may reach it
    because the tier was chosen with that maximum in view.

    Where an operator configures a tier ceiling below the normal allowance,
    the floor wins: a role that cannot read is worse than a role that reads
    a little too much.
    """
    _, commissioned, _, _ = configured_kernel_limits(
        platform, depth=depth, tier=tier
    )
    floor = int(platform.kernel_max_tool_calls)
    derived = int(platform.kernel_role_tool_calls_base) + math.ceil(
        float(platform.kernel_role_tool_calls_per_source)
        * role_source_count(handover)
    )
    return min(max(derived, floor), max(int(commissioned), floor))


def _limit_payloads(deps: KernelDeps) -> dict[str, dict[str, Any]]:
    """The three budgets of one run as one mapping, spent and configured.

    ONE projection, because the execution block a client reads and the
    proportion check a binding budget triggers must be looking at the same
    three numbers — a check reading its own copy would report a
    disproportion the surface does not show.
    """
    limits: dict[str, dict[str, Any]] = {}
    if deps.tool_call_limit > 0:
        limits["tool_calls"] = {
            "used": max(0, deps.tool_calls_used),
            "limit": deps.tool_call_limit,
            "ceiling": max(deps.tool_call_limit, deps.tool_call_ceiling),
            "recoverable": True,
            "extendable": deps.tool_call_limit < deps.tool_call_ceiling,
        }
    if deps.step_limit > 0:
        limits["steps"] = {
            "used": max(0, deps.checkpointed_steps),
            "limit": deps.step_limit,
            "ceiling": max(deps.step_limit, deps.step_ceiling),
            "recoverable": True,
            "extendable": deps.step_limit < deps.step_ceiling,
        }
    if deps.token_budget > 0:
        limits["tokens"] = {
            "used": max(0, sum(deps.run_usage().values())),
            "limit": deps.token_budget,
            "ceiling": deps.token_budget,
            "recoverable": False,
            "extendable": False,
            "reason": "operator_ceiling_exactly_once_required",
        }
    return limits


def _log_disproportionate_budget(
    deps: KernelDeps, *, run_kind: str, bound: str
) -> None:
    """Make a budget that bound for the wrong reason visible in the log.

    Nothing else reports it: each limit message states its own number, and a
    ceiling that stops runs while the costs it stands for go unspent reads
    like a run that was simply too big. *bound* names the budget that
    stopped this run; the caller that stopped it knows, and the spent count
    of a budget that rejected a whole batch does not.
    """
    if not bound:
        return
    finding = disproportionate_budget(_limit_payloads(deps), bound=bound)
    if finding:
        log.warning(
            "Run %s (%s): a budget bound while its siblings were barely "
            "spent — %s. The binding limit is not measuring this run's cost.",
            deps.run_id,
            run_kind,
            finding,
        )


def configured_kernel_limits(
    platform: Any,
    *,
    depth: str,
    tier: str,
    handover: "WorkHandover | None" = None,
) -> tuple[int, int, int, int]:
    """Return base/ceiling pairs from operator settings for one run.

    A role run (a *handover* is given) works within the budget
    :func:`role_tool_budget` derives from its subject and the step budget
    that budget prices. Neither extends: a role has no user to ask, so an
    exhausted budget is its typed outcome.

    That pricing is capped by the operator's own step ceiling for the tier.
    The two budgets bound different things and only one of them follows the
    subject: how many calls a role may spend is a THOROUGHNESS question, and
    a thorough subject deserves more; how many graph super-steps any one run
    may take is a CAPACITY question the operator answered once, to bound
    wall clock against the edge timeout its docstring names. A derived
    budget that walked past that answer would be deriving capacity, and
    nobody asked it to. Calls are spent in parallel batches, so the cap
    costs a role turns, not reads.

    The ``schnell`` tier remains intentionally non-extendable: extending its
    step budget would silently turn the advertised seconds-scale route into a
    normal research run. Users can still accept the explicit partial result
    or cancel and submit with another tier.
    """
    if handover is not None:
        tool_base = role_tool_budget(
            platform, depth=depth, tier=tier, handover=handover
        )
        _, _, _, operator_steps = configured_kernel_limits(
            platform, depth=depth, tier=tier
        )
        step_base = min(
            _ANSWER_TURN_SUPERSTEPS + _SUPERSTEPS_PER_TOOL_TURN * tool_base,
            operator_steps,
        )
        return tool_base, tool_base, step_base, step_base
    deep = depth == "deep"
    tool_base = int(
        platform.kernel_max_tool_calls_deep
        if deep
        else platform.kernel_max_tool_calls
    )
    tool_ceiling = max(
        tool_base,
        int(
            getattr(
                platform,
                (
                    "kernel_max_tool_calls_extension_ceiling_deep"
                    if deep
                    else "kernel_max_tool_calls_extension_ceiling"
                ),
                tool_base,
            )
        ),
    )
    step_base = int(
        platform.kernel_max_iterations_deep
        if deep
        else platform.kernel_max_iterations
    )
    step_ceiling = max(
        step_base,
        int(
            getattr(
                platform,
                (
                    "kernel_max_iterations_extension_ceiling_deep"
                    if deep
                    else "kernel_max_iterations_extension_ceiling"
                ),
                step_base,
            )
        ),
    )
    if tier == "schnell":
        step_base = min(
            step_base,
            _ANSWER_TURN_SUPERSTEPS
            + _SUPERSTEPS_PER_TOOL_TURN
            * (_SCHNELL_TOOL_TURNS + _SCHNELL_READ_TURNS),
        )
        step_ceiling = step_base
        tool_ceiling = tool_base
    return tool_base, tool_ceiling, step_base, step_ceiling


def _partial_limit_answer(
    gate: AgentLimitGate, references: list[dict[str, Any]], *, language: str
) -> str:
    """Build a non-generative, explicitly incomplete answer receipt.

    A limit choice must not trigger another unbounded model call. The receipt
    therefore reports only persisted evidence metadata; it makes no new claim
    and points users to the complete evidence ledger for inspection.
    """
    words = {"language": language or ""}
    limit_name = t(
        words,
        "kernel_partial_limit_name_tool_calls"
        if gate.kind == "tool_calls"
        else "kernel_partial_limit_name_steps",
    )
    lines = [
        t(words, "kernel_partial_heading"),
        "",
        t(words, "kernel_partial_intro", limit_name=limit_name),
        "",
        t(words, "kernel_partial_evidence_heading"),
        "",
    ]
    if not references:
        lines.append(t(words, "kernel_partial_no_source"))
    else:
        fallback_label = t(words, "kernel_partial_source_fallback_label")
        untitled = t(words, "kernel_partial_source_untitled")
        for reference in references[:30]:
            label = str(reference.get("label") or fallback_label).strip()
            title = str(
                reference.get("title")
                or reference.get("document_title")
                or reference.get("url")
                or untitled
            )
            # Brackets in a title would read as a Markdown link or label;
            # round brackets keep the words. A backslash escape is not an
            # option: the web renderer reads `\[ … \]` as a LaTeX display
            # delimiter and would typeset the title as an equation.
            title = " ".join(title.split()).replace("[", "(").replace("]", ")")
            lines.append(f"- [{label}] {title}")
        omitted = len(references) - 30
        if omitted > 0:
            lines.append(
                t_count(words, "kernel_partial_more_sources", omitted)
            )
    lines.extend(
        [
            "",
            t(words, "kernel_partial_gap_heading"),
            "",
            t(words, "kernel_partial_gap_text"),
        ]
    )
    return normalize_agent_markdown("\n".join(lines))


_CONTEXT_TRIGGER_FLOOR_TOKENS = 128_000
"""Auto-trigger floor AND the no-card fallback: compacting earlier than
128k would churn the prompt cache for runs that fit comfortably, and a
model without a card gets a conservative threshold instead of the
deepagents 170k default that the kernel's ceilings never reach."""


def resolve_context_trigger_tokens(
    platform: Any, model_id: str | None
) -> int:
    """Per-run compaction threshold: explicit pin > model card > floor.

    Pure resolver (no I/O): an explicit
    ``kernel_context_trigger_tokens`` pin wins verbatim; otherwise the
    resolved model's card yields ``context_window_tokens x fraction``
    bounded below by the floor; an unknown model uses the floor.
    """
    pinned = int(getattr(platform, "kernel_context_trigger_tokens", 0) or 0)
    if pinned > 0:
        return pinned
    fraction = float(
        getattr(platform, "kernel_context_trigger_fraction", 0.75) or 0.75
    )
    from inqtrix.model_cards import resolve_model_card

    card = resolve_model_card(model_id) if model_id else None
    window = int(getattr(card, "context_window_tokens", 0) or 0)
    if window <= 0:
        return _CONTEXT_TRIGGER_FLOOR_TOKENS
    if window <= _CONTEXT_TRIGGER_FLOOR_TOKENS:
        # A floor at/above the model's whole window would mean "never
        # compact before provider overflow" — small-window models use the
        # plain fraction instead.
        return int(window * fraction)
    return max(_CONTEXT_TRIGGER_FLOOR_TOKENS, int(window * fraction))


def _stage_kernel_memory(deps: KernelDeps, answer: str) -> None:
    """Stage candidate-only long-term memories after a successful run.

    Mission parity (``_stage_memory_candidates``): candidates only —
    nothing is written to memory without the user's later review. The
    kernel's substrate is its final answer (it has no memo); the shared
    ``run_memory_reflection`` + ``stage_candidates`` path is reused, so
    there is exactly ONE reflection/staging implementation. Optional by
    contract: every failure logs and returns, it can never fail a run
    that already has its answer.
    """
    service = deps.memory
    principal = deps.principal
    if (
        service is None
        or principal is None
        or not deps.memory_opt_in
        or not answer.strip()
    ):
        return
    status = service.status(principal)
    if (
        status.get("provider") == "none"
        or status.get("mode") == "off"
        or not status.get("principal_eligible")
    ):
        return
    from inqtrix.agents import memory_reflection
    from inqtrix.agents.phase_models import MemoryReflection

    provider_models = getattr(deps.llm, "models", None)
    model: str | None = None
    effort: str | None = None
    if provider_models is not None:
        desc = describe_resolution(
            "agent_memory_reflection",
            provider_models,
            "",
            requested_model="",
            requested_effort="",
        )
        model = desc.get("model") or None
        effort = desc.get("effort") or None
    try:
        outcome = memory_reflection.run_memory_reflection(
            deps.llm,
            question=deps.question,
            memo_markdown=answer,
            critic_digest="",
            task_digest="",
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - candidate staging is optional
        log.warning(
            "Kernel memory reflection failed (error_type=%s).",
            type(exc).__name__,
        )
        # Mission parity: the failure is an EVENT, not just a log line
        # (_stage_memory_candidates emits the same activity kind).
        _memory_words = {
            "language": resolve_answer_language(deps.question).code
        }
        deps.emit(
            "inqtrix.agent.activity",
            {
                "kind": "memory_unavailable",
                "label": t(_memory_words, "agent_memory_label_unavailable"),
                "detail": t(
                    _memory_words, "agent_memory_detail_candidates_failed"
                ),
            },
        )
        return
    deps.book_usage(
        outcome.usage.get("prompt_tokens", 0),
        outcome.usage.get("completion_tokens", 0),
    )
    reflection = outcome.value
    if not isinstance(reflection, MemoryReflection):
        return
    if not reflection.candidates:
        return
    try:
        staged = run_coro(
            service.stage_candidates(
                principal=principal,
                candidates=[
                    candidate.model_dump()
                    for candidate in reflection.candidates
                ],
                source_run_id=deps.run_id,
            )
        )
    except Exception as exc:  # noqa: BLE001 - candidate staging is optional
        log.warning(
            "Kernel memory staging failed (error_type=%s).",
            type(exc).__name__,
        )
        return
    log.info(
        "Kernel memory: %d candidates staged for review.",
        len(staged) if isinstance(staged, list) else 0,
    )


_INLINE_CITATION_LABEL = citation_pattern(AGENT_FAMILIES)
_INLINE_MARKDOWN_URL = re.compile(r"\[[^\]\n]+\]\((https?://[^\s)]+)\)")
_UNRESOLVED_CITATION_MARKER = re.compile(
    r"\[(?:unbelegt|unsupported):[^\]\n]*\]",
    re.IGNORECASE,
)
"""Any unsupported-citation marker, whatever it names: a single label or a
bracket no reader could parse (``[unbelegt: K3–K5]``)."""
_UNRESOLVED_HARD_CLAIM_MARKER = re.compile(
    r"\[(?:unbelegt:\s*harte\s+aussage|unsupported:\s*hard\s+claim)\]",
    re.IGNORECASE,
)


def _result_references(
    answer: str,
    ledger: list[dict[str, Any]],
    *,
    basis_fallback: bool = True,
) -> list[dict[str, Any]]:
    """References the final answer surfaces: cited-only when it cites.

    An answer that carries ``[K#]/[W#]`` labels lists EXACTLY the cited
    subset (no read-everything reference dumps). An answer without
    citation labels keeps the full trusted ledger — those sources were
    the basis of the work, and an empty list would hide them (quick and
    conversational answers rarely cite inline) — but ONLY when this run
    gathered sources itself (``basis_fallback``, the kernel passes
    ``deps.gathered_sources``). A run that merely read a document whose
    references registered in the ledger, or only wrote a file, surfaces
    no sources: a status-like answer with "21 Quellen" misattributes.
    One helper for both the answer artifact and
    ``result_state.report_references`` so the run result and the rendered
    artifact can never disagree.  Any presence of unknown inline labels
    disables the uncited-answer basis fallback: mixed answers retain only
    their known cited subset, and unknown-only answers surface no falsely
    attributed references.
    """
    from inqtrix.agents.report_quality import (
        cited_references,
        malformed_citation_brackets,
        unknown_citation_labels,
    )

    unknown = unknown_citation_labels(answer, ledger)
    malformed = malformed_citation_brackets(answer)
    unresolved_marker = bool(
        _UNRESOLVED_CITATION_MARKER.search(answer)
        or _UNRESOLVED_HARD_CLAIM_MARKER.search(answer)
    )
    if unknown:
        log.warning(
            "Answer cites unknown evidence labels (unknown_count=%d).",
            len(unknown),
        )
    cited = cited_references(answer, ledger)
    if unknown or malformed or unresolved_marker:
        return cited
    linked_urls = {
        normalize_url(match.group(1))
        for match in _INLINE_MARKDOWN_URL.finditer(answer)
        if normalize_url(match.group(1))
    }
    if linked_urls:
        return [
            dict(item)
            for item in ledger
            if normalize_url(str(item.get("url") or "")) in linked_urls
        ]
    if cited or not basis_fallback:
        return cited
    return [dict(item) for item in ledger]


def _mark_unresolved_citation_labels(
    answer: str,
    unknown: list[str],
    *,
    language: str,
) -> str:
    """Replace unresolved citation syntax with an explicit visible marker.

    An unknown label and a bracket no reader can parse (a range, a slash, a
    prose join) both become ``[unbelegt: ...]`` with their text inside; a
    range is marked, never expanded.
    """
    unresolved = set(unknown)
    marker = t({"language": language}, "kernel_citation_marker_unsupported")
    answer = mark_malformed_citations(answer, marker)
    return _INLINE_CITATION_LABEL.sub(
        lambda match: (
            f"[{marker}: {match.group(1)}]"
            if match.group(1) in unresolved
            else match.group(0)
        ),
        answer,
    )


def _validate_kernel_answer_citations(
    deps: KernelDeps,
    answer: str,
    ledger: list[dict[str, Any]],
) -> str:
    """Boundedly repair final chat citations or mark unresolved labels.

    Canvas writes return a citation error to the bounded kernel loop, giving
    the model one chance to correct the tool call.  A tool-free final chat
    answer has no next loop turn, so it uses the shared synthesis repair once.
    If that call fails or still invents labels, only the unsupported citation
    syntax is replaced with a visible ``unbelegt`` marker; valid labels and
    prose remain unchanged. Internal identifiers this answer INTRODUCED use
    the same bounded repair; an unusable metadata repair is rejected after
    metering rather than publishing the internal catalog as an answer. An
    identifier the user themselves named stays: it is their word, and
    repairing it away would answer a different question than the one asked.
    """
    from inqtrix.agents.prompts import agent_answer_system_prompt
    from inqtrix.agents.report_quality import (
        CitationValidationFailed,
        cited_references,
        introduced_identifiers,
        malformed_citation_brackets,
        unknown_citation_labels,
        validate_and_repair_citations,
        writer_identifiers,
    )
    from inqtrix.exceptions import AgentCancelled
    from inqtrix.execution_authority import AuthorizationRevoked

    unknown = unknown_citation_labels(answer, ledger)
    malformed = malformed_citation_brackets(answer)
    internal_ids = writer_identifiers(ledger)
    authored_by = deps.user_text()
    leaked_ids = introduced_identifiers(answer, internal_ids, authored_by=authored_by)
    if not unknown and not malformed and not leaked_ids:
        return answer
    language = detect_ui_language(deps.question)

    deps.check_abort()
    known_labels = [
        str(reference.get("label") or "")
        for reference in ledger
        if str(reference.get("label") or "")
    ]
    try:
        repaired, usage = validate_and_repair_citations(
            deps.llm,
            markdown=answer,
            known_labels=known_labels,
            internal_reference_ids=internal_ids,
            authored_by=authored_by,
            usage={},
            system=agent_answer_system_prompt(),
            model=deps.model,
            reasoning_effort=deps.reasoning_effort,
            timeout=deps.timeout,
        )
    except CitationValidationFailed as exc:
        deps.book_usage(
            exc.usage.get("prompt_tokens", 0),
            exc.usage.get("completion_tokens", 0),
        )
        if leaked_ids:
            raise
        remaining = unknown_citation_labels(answer, ledger)
        marked = _mark_unresolved_citation_labels(
            answer,
            remaining,
            language=language,
        )
        deps.emit(
            "inqtrix.agent.citation.validation",
            {
                "status": "degraded",
                "unknown_labels": remaining,
                "malformed_brackets": malformed,
                "resolution": "marked_unsubstantiated",
            },
        )
        return marked
    except (AgentCancelled, AuthorizationRevoked):
        raise
    except Exception as exc:  # noqa: BLE001 — deterministic safe degradation
        if leaked_ids:
            raise CitationValidationFailed(
                "Internal reference metadata could not be removed from the final answer.",
                usage={},
            ) from exc
        log.warning(
            "Bounded citation repair failed (error_type=%s); unresolved "
            "labels are marked.",
            type(exc).__name__,
        )
        remaining = unknown_citation_labels(answer, ledger)
        marked = _mark_unresolved_citation_labels(
            answer,
            remaining,
            language=language,
        )
        deps.emit(
            "inqtrix.agent.citation.validation",
            {
                "status": "degraded",
                "unknown_labels": remaining,
                "malformed_brackets": malformed,
                "resolution": "marked_unsubstantiated",
            },
        )
        return marked

    deps.book_usage(
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
    )
    deps.emit(
        "inqtrix.agent.citation.validation",
        {
            "status": "repaired",
            "unknown_labels": unknown,
            "malformed_brackets": malformed,
            "resolution": "bounded_repair",
        },
    )
    if (unknown or malformed) and not cited_references(repaired, ledger):
        # A valid repair may remove an unsupported label when no honest
        # replacement exists. Preserve that fact in the rendered answer so
        # the reference projector cannot mistake it for an originally
        # citation-free answer and attach the whole ledger.
        words = {"language": language}
        marker = t(words, "kernel_citation_marker_unsupported")
        markers = ", ".join(
            [f"[{marker}: {label}]" for label in unknown]
            + [f"[{marker}: {bracket[1:-1].strip()}]" for bracket in malformed]
        )
        notice = t(words, "kernel_citation_removed_notice", markers=markers)
        repaired = normalize_writer_markdown(f"{repaired}\n\n{notice}")
    return repaired


def _show_answer_draft(deps: KernelDeps, answer: str) -> None:
    """Show the reader the answer the review is about to check.

    The draft is the answer after the citation check the published answer
    passes too, emitted whole as one durable event before the review
    starts; the published answer replaces it when it is ready. The review
    itself reads *answer* as the model wrote it, which the checkpoint
    reproduces on every segment, while the check may repair through a model
    call that would not. A segment that re-enters after its review role
    parked the run showed the draft before the park. A draft whose check
    fails is not shown; the published answer passes the same check again.
    """
    from inqtrix.agents.kernel.review_pass import role_child_known
    from inqtrix.agents.report_quality import CitationValidationFailed, cited_references
    from inqtrix.runs.shared import ANSWER_DRAFT_EVENT, answer_artifact_id, answer_draft_id

    if role_child_known(deps.run_service, deps.run_id):
        return
    ledger = list(deps.evidence_refs.values())
    try:
        draft = _validate_kernel_answer_citations(deps, answer, ledger)
    except CitationValidationFailed as exc:
        log.warning(
            "Run %s: the answer draft is not shown; its citation check "
            "failed (error_type=%s). The reader waits for the reviewed answer.",
            deps.run_id,
            type(exc).__name__,
        )
        return
    deps.emit(
        ANSWER_DRAFT_EVENT,
        {
            "artifact_id": answer_artifact_id(deps.run_id),
            "draft_id": answer_draft_id(deps.run_id),
            "markdown": draft,
            "reference_labels": [
                label
                for ref in cited_references(draft, ledger)
                if (label := str(ref.get("label") or "").strip())
            ],
        },
    )


def _request_user_text(request: "RunRequest") -> str:
    """The user's own words on the durable request, beside the question.

    Read from the request rather than from the hydrated session context:
    the payload is rebuilt from storage for every attempt, while the
    context pack is built only where a segment composes a user message.
    The canvas comment carries the passage the user selected AND what they
    wrote about it - both are their text, and an id may stand in either. On
    a SHARED canvas the commenter may be a colleague rather than the run's
    own user; the guard's question is whether a person wrote the id, not
    which person, so a collaborator's comment counts like the user's own.

    ``history`` is a transcript of both voices and is projected to its user
    lines; everything else here is wholly user-written.
    """
    from inqtrix.agents.report_quality import user_authored_text

    canvas = request.canvas_context
    return user_authored_text(
        *(
            text for text in (
                request.report_requirement,
                *(
                    f"{comment.quote}\n{comment.comment}"
                    for comment in (canvas.comments if canvas is not None else ())
                ),
            ) if text
        ),
        history=request.history,
    )


def _kernel_consent_reason(deps: KernelDeps) -> str:
    """Canonical consent-policy label before any per-call decision."""
    return (
        "autonomous_policy"
        if deps.autonomy == "autonomous"
        else "permission_policy"
    )


def _asserted_execution_snapshot(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """A tool-boundary snapshot must be the COMPLETE triple: clients
    replace all three layers on arrival, so a partial dict would erase
    the layers it omits."""
    missing = {"current_node", "phase", "execution"} - set(snapshot)
    if missing:
        raise ValueError(
            f"execution snapshot missing keys: {sorted(missing)}"
        )
    return snapshot


def _observe_update(
    deps: KernelDeps,
    update: dict[str, Any],
    execution_snapshot: Callable[[], dict[str, Any]],
) -> list[Any]:
    """Emit follow-the-agent events from one stream update; return interrupts.

    THE one place the raw update stream is interpreted: tool identity ->
    deterministic localized narration and started event (redacted args
    preview), tool results -> finished (with the fresh execution snapshot,
    so the desk's limit readouts advance at tool boundaries instead of
    only at phase changes), todo state -> todo.updated. Model prose never
    becomes narration. Events are signals — clients refetch, so a
    replayed duplicate is harmless.
    """
    if "__interrupt__" in update:
        return list(update["__interrupt__"])
    for delta in update.values():
        if not isinstance(delta, dict):
            continue
        for message in delta.get("messages") or []:
            role = getattr(message, "type", "")
            if role == "ai":
                tool_calls = getattr(message, "tool_calls", None) or []
                message_id = str(getattr(message, "id", "") or "")
                # No intent sentence here (was: four fixed phrases chosen
                # by tool set, re-appended every turn). The tool row below
                # carries tool identity and the argument preview — the same
                # facts, once, derived from the accepted call, never from
                # provider prose.
                #
                # A todo update travels in the SAME turn as the call it
                # frames (the doctrine asks for exactly that), so the list
                # the sibling rows should point at is in this message's
                # write_todos arguments — not in the list the graph will
                # only report after the tool node ran.
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    if str(call.get("name", "")) != "write_todos":
                        continue
                    todo_args = call.get("args", {})
                    listed = (
                        todo_args.get("todos")
                        if isinstance(todo_args, dict)
                        else None
                    )
                    if isinstance(listed, list):
                        deps.todo_in_progress_index = _todo_in_progress_index(
                            listed
                        )
                for index, call in enumerate(tool_calls):
                    if not isinstance(call, dict):
                        continue
                    invocation_id = _tool_call_invocation_id(
                        message_id, call, index
                    )
                    if invocation_id in deps.emitted_tool_start_ids:
                        continue
                    deps.emitted_tool_start_ids[invocation_id] = str(
                        call.get("name", "") or ""
                    )
                    # Derivation-true live advance: each NEW invocation
                    # in the update stream is one checkpointed model tool
                    # call. Segment starts overwrite this from the
                    # checkpoint, and the dedup set (seeded from the same
                    # checkpoint) keeps replays from double-counting.
                    if str(call.get("name", "") or "") not in _result_call_names(
                        deps.work_profile
                    ):
                        deps.tool_calls_used += 1
                        deps.tool_call_ordinals[invocation_id] = deps.tool_calls_used
                    args = call.get("args", {})
                    args = args if isinstance(args, dict) else {}
                    preview = (
                        _context_args_preview(args, deps)
                        if call.get("name") in {"read_context", "search_context"}
                        else _evidence_args_preview(args, deps)
                        if call.get("name") == "read_evidence"
                        else _canvas_args_preview(args, deps)
                        if call.get("name") == "read_canvas"
                        else _document_args_preview(args, deps)
                        if call.get("name") in {"read_project_document", "read_editor_document"}
                        else _cite_args_preview(args, deps)
                        if call.get("name") == "cite_document_passages"
                        else _report_args_preview(args, deps)
                        if call.get("name") == "read_research_report"
                        else _args_preview(args)
                    )
                    document_id = (
                        _canvas_document_id(args, deps)
                        if call.get("name") == "read_canvas"
                        else ""
                    )
                    deps.emit(
                        TOOL_STARTED_EVENT,
                        {
                            "tool": str(call.get("name", "")),
                            "tool_call_id": invocation_id,
                            "invocation_id": invocation_id,
                            "args_preview": preview,
                            # The client names a canvas document by the
                            # title it knows; the id travels as data.
                            **({"artifact_id": document_id} if document_id else {}),
                            # Which todo this call serves — the first
                            # in_progress item of the last reported list.
                            **(
                                {"todo_index": deps.todo_in_progress_index}
                                if deps.todo_in_progress_index is not None
                                else {}
                            ),
                        },
                    )
            elif role == "tool":
                if (getattr(message, "additional_kwargs", None) or {}).get(REJECTED_TOOL_ARGUMENTS_FLAG):
                    continue
                content = getattr(message, "content", "")
                invocation_id = str(
                    getattr(message, "tool_call_id", "") or ""
                )
                if not invocation_id:
                    invocation_id = str(getattr(message, "id", "") or "")
                if invocation_id in deps.emitted_tool_finish_ids:
                    continue
                deps.emitted_tool_finish_ids.add(invocation_id)
                # Coverage travels in the checkpointed tool artifact, never
                # in relayed text: the ledger the judge and the snapshot
                # read advances here for NEW results; segment starts
                # rebuild it from the checkpoint (same dedup as above).
                deps.source_coverage.absorb_many(
                    coverage_records_from_artifact(getattr(message, "artifact", None))
                )
                finished_tool = str(
                    getattr(message, "name", "")
                    or deps.emitted_tool_start_ids.get(invocation_id, "")
                )
                ordinal = deps.tool_call_ordinals.get(invocation_id)
                deps.emit(
                    TOOL_FINISHED_EVENT,
                    {
                        "tool": finished_tool,
                        # A role's typed result is no step of its work: a
                        # delegating parent never shows it as one.
                        **(
                            {"result_call": True}
                            if finished_tool in _result_call_names(deps.work_profile)
                            else {}
                        ),
                        # Which call this is, so a delegating parent samples
                        # a batch of parallel calls by position, not by the
                        # one counter they share.
                        **({"call_ordinal": ordinal} if ordinal is not None else {}),
                        "tool_call_id": invocation_id,
                        "invocation_id": invocation_id,
                        # A failure the model reads as text is no finished
                        # step: the row settles as failed (the one
                        # contract, SOURCE_TOOL_FAILURE_PREFIXES).
                        "status": (
                            "error"
                            if failed_tool_message(message)
                            else str(getattr(message, "status", "") or "success")
                        ),
                        "result_preview": _visible_clip(str(content)),
                        "snapshot": execution_snapshot(),
                    },
                )
        todos = delta.get("todos")
        if isinstance(todos, list):
            deps.todo_in_progress_index = _todo_in_progress_index(todos)
            deps.emit(
                TODO_UPDATED_EVENT,
                {
                    "todos": [
                        {
                            "content": str(item.get("content", "")),
                            "status": str(item.get("status", "")),
                        }
                        for item in todos
                        if isinstance(item, dict)
                    ]
                },
            )
    return []


def _tool_call_invocation_id(
    message_id: str,
    call: dict[str, Any],
    ordinal: int,
) -> str:
    """Return the provider id or a deterministic fallback invocation id."""
    explicit = str(call.get("id", "") or "")
    if explicit:
        return explicit
    identity = {
        "args": call.get("args", {}),
        "message_id": message_id,
        "name": str(call.get("name", "") or ""),
        "ordinal": ordinal,
    }
    digest = hashlib.sha1(
        json.dumps(
            identity,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"call_{digest}"


def _tool_approval_id(run_id: str, interrupt_id: str) -> str:
    """Deterministic approval id of one HITL gate.

    The langgraph interrupt id is stable across re-entry and rebuilt
    agents (harness contract test) and already hash-shaped — no extra
    digest needed."""
    return f"apr_{run_id[-12:]}_tool_{interrupt_id[:12]}"


def _hitl_decisions(
    approval: Any, *, action_count: int
) -> list[dict[str, Any]]:
    """Map one decided approval row onto HITL resume decisions.

    Approve/reject fan out over ALL actions of the gate (one row
    approves the whole batch); an edit is validated to exactly one
    action by the service, so its mapping is single-action by
    construction.
    """
    if approval.decision == "approve":
        return [{"type": "approve"} for _ in range(action_count)]
    if approval.decision == "reject":
        decision: dict[str, Any] = {"type": "reject"}
        if approval.note:
            decision["message"] = approval.note
        return [dict(decision) for _ in range(action_count)]
    if approval.decision == "edit":
        edited = (approval.decision_payload.get("actions") or [{}])[0]
        return [
            {
                "type": "edit",
                "edited_action": {
                    "name": str(edited.get("tool", "")),
                    "args": dict(edited.get("args") or {}),
                },
            }
        ]
    raise RuntimeError(
        f'Unknown tool decision {approval.decision!r} for approval {approval.approval_id}.'
    )


def _checkpointed_usage(snapshot: Any) -> dict[str, int]:
    """Recover known cumulative spend from the same durable checkpoint.

    The kernel aggregate includes auxiliary model calls and other recorded
    kernel work. Graphs without that aggregate use their message receipts.
    An invalid aggregate is an error, never a silent reset of the budget.
    """
    totals = {"prompt_tokens": 0, "completion_tokens": 0}
    if snapshot is None or not snapshot.values:
        return totals
    if KERNEL_USAGE_STATE in snapshot.values:
        stored = snapshot.values[KERNEL_USAGE_STATE]
        if not isinstance(stored, dict) or any(
            type(stored.get(key)) is not int or stored[key] < 0 for key in totals
        ):
            raise ValueError("Checkpointed kernel usage is invalid.")
        return {key: stored[key] for key in totals}
    for message in snapshot.values.get("messages") or []:
        meta = getattr(message, "usage_metadata", None) or {}
        totals["prompt_tokens"] += int(meta.get("input_tokens", 0) or 0)
        totals["completion_tokens"] += int(
            meta.get("output_tokens", 0) or 0
        )
    return totals


def _reactivate_editor_read_receipts(deps: KernelDeps, snapshot: Any) -> None:
    """Rebuild editor read receipts from checkpointed ToolMessages.

    Same trust template as ``_reactivate_loaded_skills``: only the
    PRODUCING TOOLS mint receipts — a marker-shaped line relayed by any
    other tool (web answers carry attacker-controlled text verbatim)
    must never count as a read. Unlike a lost skill RESTRICTION, a lost
    receipt is only a lost PERMISSION: a corrupt marker is skipped with
    a warning and the model simply has to read again — no abort.
    """
    from inqtrix.agents.kernel.tools import (
        EDITOR_READ_MARKER,
        EDITOR_READ_TOOLS,
    )

    if snapshot is None or not snapshot.values:
        return
    prefix = EDITOR_READ_MARKER.split("{", 1)[0]
    for message in snapshot.values.get("messages") or []:
        if getattr(message, "type", "") != "tool":
            continue
        if getattr(message, "name", "") not in EDITOR_READ_TOOLS:
            continue
        content = str(getattr(message, "content", "") or "")
        if not content.startswith(prefix):
            continue
        marker_body = content[len(prefix):].split("]", 1)[0]
        document_id, separator, revision = marker_body.rpartition("@")
        if not document_id or not separator or not revision.isdigit():
            log.warning(
                "Editor read receipt has no valid checkpoint marker; it is not "
                "reconstructed, and a new read is required."
            )
            continue
        deps.editor_read_receipts[document_id] = int(revision)


def _checkpointed_steps(snapshot: Any) -> int:
    """Return the cumulative committed LangGraph super-step coordinate."""
    if snapshot is None:
        return 0
    metadata = getattr(snapshot, "metadata", None) or {}
    try:
        # LangGraph starts at -1 before the first committed graph step.
        return max(0, int(metadata.get("step", -1)) + 1)
    except (TypeError, ValueError):
        return 0


def _checkpointed_tool_call_count(
    snapshot: Any, *, uncounted: frozenset[str] = frozenset()
) -> int:
    """Count all model-requested calls from checkpointed AI messages."""
    if snapshot is None or not snapshot.values:
        return 0
    from inqtrix.agents.kernel.middleware import requested_tool_calls

    return requested_tool_calls(
        snapshot.values.get("messages") or [], uncounted=uncounted
    )


def _checkpointed_tool_event_ids(
    snapshot: Any,
) -> tuple[dict[str, str], set[str]]:
    """Reconstruct durable logical tool events from checkpointed messages."""
    starts: dict[str, str] = {}
    finishes: set[str] = set()
    if snapshot is None or not snapshot.values:
        return starts, finishes
    for message in snapshot.values.get("messages") or []:
        if isinstance(message, dict):
            role = str(message.get("role") or message.get("type") or "")
            message_id = str(message.get("id", "") or "")
            calls = message.get("tool_calls") or []
            tool_call_id = str(message.get("tool_call_id", "") or "")
        else:
            role = str(getattr(message, "type", "") or "")
            message_id = str(getattr(message, "id", "") or "")
            calls = getattr(message, "tool_calls", None) or []
            tool_call_id = str(
                getattr(message, "tool_call_id", "") or ""
            )
        if role in {"ai", "assistant"}:
            for index, call in enumerate(calls):
                if isinstance(call, dict):
                    starts[
                        _tool_call_invocation_id(message_id, call, index)
                    ] = str(call.get("name", "") or "")
        elif role == "tool":
            additional = (
                message.get("additional_kwargs", {})
                if isinstance(message, dict)
                else getattr(message, "additional_kwargs", {})
            )
            if additional.get(REJECTED_TOOL_ARGUMENTS_FLAG):
                continue
            finishes.add(tool_call_id or message_id)
    finishes.discard("")
    return starts, finishes


def _checkpointed_tool_use_counts(snapshot: Any) -> dict[str, int]:
    """Rehydrate cumulative source-tool usage from persisted tool messages.

    SUCCESS-only, mirroring the live counter (which increments after a
    successful invoke): failed calls persist as visible failure texts
    (``SOURCE_TOOL_FAILURE_PREFIXES``) or error-status ToolMessages and
    must not consume budgets or arm the sufficiency judge on resume.
    """
    from inqtrix.agents.source_policy import (
        KNOWLEDGE_TOOL_NAMES,
        UNCOUNTED_SOURCE_TOOLS,
        WEB_TOOL_NAMES,
    )

    counts = {"web": 0, "knowledge": 0}
    for name in _succeeded_tool_names(snapshot):
        if name in UNCOUNTED_SOURCE_TOOLS:
            continue
        if name in WEB_TOOL_NAMES:
            counts["web"] += 1
        elif name in KNOWLEDGE_TOOL_NAMES:
            counts["knowledge"] += 1
    return counts


def _checkpointed_web_searches(snapshot: Any) -> int:
    """Rehydrate the instant web searches this run executed itself.

    The live counter's rule (:attr:`KernelDeps.web_searches`): one per
    successful ``web_instant``. A delegated research is a budget use of the
    web, not a search of this run; its child reports its own.
    """
    return sum(1 for name in _succeeded_tool_names(snapshot) if name == "web_instant")


def _succeeded_tool_names(snapshot: Any) -> Iterator[str]:
    """The names of the checkpointed tool calls that succeeded, in order."""
    if snapshot is None or not snapshot.values:
        return
    for message in snapshot.values.get("messages") or []:
        if getattr(message, "type", "") != "tool" or failed_tool_message(message):
            continue
        yield str(getattr(message, "name", "") or "")


def _checkpointed_source_coverage(snapshot: Any) -> SourceCoverageLedger:
    """Rebuild the source coverage ledger from checkpointed tool artifacts.

    Only the PRODUCING READERS attach coverage records, and only as the
    checkpointed ``ToolMessage.artifact`` (the source-receipt template):
    relayed text can neither add nor close coverage. Error-status
    messages carry no records by construction, so nothing is filtered by
    text here.
    """
    ledger = SourceCoverageLedger()
    if snapshot is None or not snapshot.values:
        return ledger
    for message in snapshot.values.get("messages") or []:
        if getattr(message, "type", "") != "tool":
            continue
        ledger.absorb_many(
            coverage_records_from_artifact(getattr(message, "artifact", None))
        )
    return ledger


def _recover_clarified_answers(deps: KernelDeps, snapshot: Any) -> None:
    """Re-read what the user already answered, from the checkpoint.

    ``deps`` is rebuilt for EVERY segment, and every approval gate starts
    a new one — so an answer given before a gate was gone by the time the
    coverage judge ran, and the judge reported a gap the user had already
    closed. The answer itself is durable: it is the ToolMessage
    ``ask_user`` returned. Read it back like the loaded-skill markers,
    and trust the PRODUCING TOOL rather than the text — other tools
    relay provider-controlled text verbatim.
    """
    if snapshot is None or not snapshot.values:
        return
    for message in snapshot.values.get("messages") or []:
        if getattr(message, "type", "") != "tool":
            continue
        if getattr(message, "name", "") != "ask_user":
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if not content or content not in deps.clarified_answers:
            if content:
                deps.clarified_answers.append(content)
