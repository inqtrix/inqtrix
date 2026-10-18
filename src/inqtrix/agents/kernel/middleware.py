"""Inqtrix middleware enforcing kernel-owned execution contracts."""

from __future__ import annotations

import contextvars
import hashlib
import logging
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, ExtendedModelResponse, PrivateStateAttr
from langchain_core.messages import AIMessage, HumanMessage

from inqtrix.agents.prompts import HARNESS_NOTE_PREFIX, POST_MISSION_EVIDENCE_GUIDANCE
from inqtrix.agents.kernel.chat_bridge import KERNEL_USAGE_STATE, collect_model_usage
from inqtrix.agents.narration import emit_narration
from inqtrix.i18n import t
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.untrusted_content import untrusted_fence

log = logging.getLogger("inqtrix")

TOOL_LIMIT_REACHED_EVENT = "inqtrix.agent.tool_limit.reached"
SKILL_INPUTS_RESOLVED_MARKER = "[skill_inputs_resolved]"


def _decision_answer(
    question: dict[str, Any], decision: dict[str, Any]
) -> str:
    entry = (decision.get("answers") or {}).get(question.get("id", "")) or {}
    labels = {
        option["id"]: option["label"]
        for option in question.get("options", [])
    }
    parts = [
        labels[item]
        for item in entry.get("option_ids", [])
        if item in labels
    ]
    text = str(entry.get("text") or "").strip()
    if text:
        parts.append(text)
    if parts:
        return ", ".join(parts)
    return str(decision.get("answer") or "").strip()


def resolve_kernel_skill_inputs(
    skill: Any,
    *,
    clarification_scope: str,
) -> dict[str, str]:
    """Resolve required skill points before activating its instructions.

    The same resolver serves attached skills and dynamic ``load_skill``.
    Missing points become deterministic clarification rows and LangGraph
    interrupts; the skill remains inactive until every batch is answered.
    """
    from langgraph.types import interrupt

    from inqtrix.agents.clarification import (
        build_clarification,
        guarded_questions,
        sanitize_questions,
    )
    from inqtrix.agents.control_ports import ClarificationNotFound
    from inqtrix.agents.kernel.deps import kernel_deps, run_coro
    from inqtrix.agents.phase_models import (
        ClarificationOptionModel,
        ClarificationQuestionModel,
    )
    from inqtrix.agents.skills_runtime import (
        check_skill_points,
        skill_point_key,
        unanswered_required_points,
    )
    from inqtrix.model_routing import (
        describe_resolution,
        describe_unresolved_resolution,
    )

    deps = kernel_deps()
    provider_models = getattr(deps.llm, "models", None)
    resolution = (
        describe_unresolved_resolution("agent_skill_point_check", "")
        if provider_models is None
        else describe_resolution(
            "agent_skill_point_check",
            provider_models,
            "",
            requested_model="",
            requested_effort="",
        )
    )
    deps.emit("inqtrix.node.model_resolution", resolution)
    answers, usage = check_skill_points(
        deps.llm,
        skill=skill,
        question=deps.question,
        history=deps.session_history,
        model=resolution.get("model") or None,
        reasoning_effort=resolution.get("effort") or None,
        timeout=deps.timeout,
    )
    deps.book_usage(
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    )
    missing = unanswered_required_points(skill, answers)
    for batch_index in range(0, len(missing), 3):
        points = missing[batch_index : batch_index + 3]
        questions = sanitize_questions(
            [
                ClarificationQuestionModel(
                    prompt=str(point.get("question") or ""),
                    options=[
                        ClarificationOptionModel(
                            label=str(option.get("label") or ""),
                            description=str(option.get("description") or ""),
                        )
                        for option in point.get("options", [])
                    ],
                    multi_select=bool(point.get("multi_select")),
                )
                for point in points
            ]
        )
        for question, point in zip(questions, points, strict=True):
            question["skill_key"] = skill_point_key(point)
        scope_hash = hashlib.sha1(
            clarification_scope.encode("utf-8")
        ).hexdigest()[:10]
        clarification_id = (
            f"clr_{deps.run_id[-12:]}_skill_{scope_hash}_{batch_index // 3}"
        )
        try:
            record = run_coro(
                deps.control.get_clarification(deps.run_id, clarification_id)
            )
        except ClarificationNotFound:
            record = run_coro(
                deps.control.create_clarification(
                    build_clarification(
                        deps.run_id,
                        questions=guarded_questions(
                            questions, deps.guard_reader_text
                        ),
                        clarification_id=clarification_id,
                    )
                )
            )
            deps.emit(
                "inqtrix.agent.clarification.requested",
                {
                    "clarification_id": record.clarification_id,
                    "question": record.question,
                    "options": [dict(item) for item in record.options],
                    "question_count": len(record.questions),
                },
            )
        if record.status == "answered":
            decision = {
                "answer": record.answer,
                "option_id": record.option_id,
                "answers": dict(record.answers),
            }
        else:
            decision = interrupt(
                {"kind": "clarification", "id": clarification_id}
            )
        for question in record.questions:
            value = _decision_answer(dict(question), dict(decision or {}))
            if value:
                answers[str(question.get("skill_key") or "")] = value
    return answers


SKILL_INPUTS_RESOLVED_FLAG = "inqtrix_skill_inputs_resolved"
"""``additional_kwargs`` key marking the middleware-authored skill block.

The TRUST anchor: user input reaches the transcript only as plain
content strings, so a user question containing the literal marker text
can neither suppress skill resolution nor smuggle a block past the deep
verifier — both key on this server-set message attribute, never on a
content substring."""


class KernelSkillInputMiddleware(AgentMiddleware):
    """Gate attached skills and inject only fully resolved skill blocks."""

    def before_model(
        self, state: dict[str, Any], runtime: Any
    ) -> dict[str, Any] | None:
        del runtime
        messages = state.get("messages") or []
        # Idempotence anchors are SERVER-SET metadata only: the
        # middleware's own flagged HumanMessage, or a load_skill
        # ToolMessage (dynamic activation resolves inputs inside the
        # tool — re-resolving here would re-ask under the attached
        # scope). User content containing the marker text is inert.
        if any(
            (getattr(message, "additional_kwargs", None) or {}).get(
                SKILL_INPUTS_RESOLVED_FLAG
            )
            or (
                type(message).__name__ == "ToolMessage"
                and getattr(message, "name", "") == "load_skill"
            )
            for message in messages
        ):
            return None
        from inqtrix.agents.kernel.deps import kernel_deps
        from inqtrix.agents.skills_runtime import build_skills_block

        try:
            deps = kernel_deps()
        except RuntimeError:
            # Low-level harness contract tests intentionally assemble a
            # throwaway graph without per-run deps.
            return None
        if not deps.skills:
            return None
        resolved: dict[str, dict[str, str]] = {}
        for skill in deps.skills:
            resolved[skill.id] = resolve_kernel_skill_inputs(
                skill,
                clarification_scope=f"attached:{skill.id}",
            )
        deps.skill_answers.update(resolved)
        block = build_skills_block(deps.skills, resolved)
        return {
            "messages": [
                HumanMessage(
                    # The marker stays as human-readable prose; the FLAG
                    # is the machine-trust anchor (checkpointed with the
                    # message, unforgeable through user content). The
                    # harness-note prefix names the provenance for the
                    # model: runtime, not user.
                    content=f"{HARNESS_NOTE_PREFIX.rstrip()}\n{SKILL_INPUTS_RESOLVED_MARKER}\n{block}",
                    additional_kwargs={SKILL_INPUTS_RESOLVED_FLAG: True},
                )
            ]
        }


SUFFICIENCY_NUDGE_FLAG = "inqtrix_sufficiency_nudge"
POST_MISSION_NUDGE_FLAG = "inqtrix_post_mission_nudge"
POST_MISSION_NUDGE_TEXT = (
    HARNESS_NOTE_PREFIX
    + "The mission is complete and its memo is available as a document in "
    "this session. "
    + POST_MISSION_EVIDENCE_GUIDANCE
    + " Then answer with a short conclusion that cites the memo's "
    "evidence labels, plus the reference to the document; evidence notes "
    "of the children you name as open points in reader language."
)
"""``additional_kwargs`` key of one advisory sufficiency nudge message.

Carries the evidence state (successful source-tool count) the verdict
was formed over. Both idempotency anchors key on it: the same state is
never judged twice (park/resume replays included), and the flagged
messages ARE the run-wide judgement count — checkpointed with the
transcript, unforgeable through user content (the same pattern as
``SKILL_INPUTS_RESOLVED_FLAG``)."""


class KernelSufficiencyMiddleware(AgentMiddleware):
    """Advisory evidence-sufficiency nudge before the next model turn.

    After enough successful source-tool calls (adaptive threshold, deep
    raises it), one cheap fast-tier judgement summarizes coverage and a
    flagged ``HumanMessage`` advises the model to either answer now or
    search ONLY for the named gaps. Advisory by design: the hard stops
    stay the tool budget and the recursion ceiling. The nudge can never
    become the final answer (``_final_answer`` accepts only AI
    messages) and runs ``before_model``, so it never breaks a pending
    tool call. Simple runs below the threshold NEVER pay a judge call.
    """

    def before_model(
        self, state: dict[str, Any], runtime: Any
    ) -> dict[str, Any] | None:
        del runtime
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            deps = kernel_deps()
        except RuntimeError:
            # Low-level harness contract tests intentionally assemble a
            # throwaway graph without per-run deps.
            return None
        if deps.post_mission_nudge_pending:
            # One advisory after the mission: reuse its memo and evidence;
            # further research must address a concrete remaining gap.
            deps.post_mission_nudge_pending = False
            return {
                "messages": [
                    HumanMessage(
                        content=POST_MISSION_NUDGE_TEXT,
                        additional_kwargs={POST_MISSION_NUDGE_FLAG: True},
                    )
                ]
            }
        platform = deps.platform
        if not platform.kernel_sufficiency_gate or deps.tier == "schnell":
            return None
        uses = deps.tool_use_counts.get("web", 0) + deps.tool_use_counts.get(
            "knowledge", 0
        )
        threshold = (
            platform.kernel_sufficiency_min_tool_calls_deep
            if deps.depth == "deep"
            else platform.kernel_sufficiency_min_tool_calls
        )
        if uses < threshold:
            return None
        judged_states = [
            kwargs.get(SUFFICIENCY_NUDGE_FLAG)
            for message in (state.get("messages") or [])
            if (
                kwargs := getattr(message, "additional_kwargs", None) or {}
            ).get(SUFFICIENCY_NUDGE_FLAG)
            is not None
        ]
        if len(judged_states) >= platform.kernel_sufficiency_max_judgements:
            return None
        if uses in judged_states:
            # This evidence state was already judged — a park/resume
            # replay or a turn without new source evidence.
            return None
        from inqtrix.agents.kernel.cognition import (
            SUFFICIENCY_JUDGED_EVENT,
            judge_kernel_sufficiency,
        )
        from inqtrix.exceptions import AgentCancelled

        from inqtrix.observability import semconv
        from inqtrix.observability.otel import operation_span

        try:
            # Real span around the judge LLM call: its generation child
            # nests here, and the verdict marker lands on the span.
            with operation_span(
                "kernel.judge", {semconv.INQTRIX_STEP: "sufficiency_judge"}
            ) as judge_span:
                outcome = judge_kernel_sufficiency(deps)
                if judge_span is not None:
                    judge_span.set_attribute(
                        semconv.INQTRIX_JUDGE_MARKER,
                        str(getattr(outcome, "marker", "") or ""),
                    )
        except AgentCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 — advisory, never fatal
            # A hard provider failure must not fail a run the judge only
            # ADVISES: visible in the event stream, then unadvised on.
            log.warning(
                "Sufficiency judge failed (error_type=%s); the run continues without "
                "its advice.",
                type(exc).__name__,
            )
            deps.emit(
                SUFFICIENCY_JUDGED_EVENT,
                {"marker": "error", "nudge": False, "tool_uses": uses},
            )
            return None
        verdict = outcome.value
        if verdict is None:
            # No-silent-fallback: the failed judgement stays visible in
            # the event stream; the loop continues UNADVISED (budget and
            # recursion remain the hard stops).
            deps.emit(
                SUFFICIENCY_JUDGED_EVENT,
                {"marker": outcome.marker, "nudge": False, "tool_uses": uses},
            )
            return None
        coverage = str(getattr(verdict, "coverage", "") or "")
        missing = [
            str(item) for item in (getattr(verdict, "missing", None) or [])
        ]
        # Sources the model started reading and has not finished:
        # derived from the checkpointed coverage ledger, never from the
        # judge's prose. Advice never overrides the assignment, so a
        # "covered" verdict must not order the model to abandon them.
        open_sources = deps.source_coverage.open_entries()
        remainders = deps.source_coverage.remainder_summary()
        deps.emit(
            SUFFICIENCY_JUDGED_EVENT,
            {
                "marker": outcome.marker,
                "nudge": True,
                "tool_uses": uses,
                "coverage": coverage,
                "missing": missing,
                "open_continuations": len(open_sources),
                "open_sources": [
                    f"{entry['kind']}:{entry['source_id']}" for entry in open_sources
                ],
            },
        )
        if coverage == "covered" and open_sources:
            text = (
                "Interim evidence state: the registered evidence covers the "
                f"assignment, but these sources have unread remainders: {remainders}. "
                "Continue reading if the assignment requires the complete source; "
                "otherwise answer now and name the unread remainder visibly in the answer."
            )
        elif coverage == "covered":
            text = (
                "Interim evidence state: sufficient for the assignment. "
                "Stop researching and formulate the answer now on the "
                "basis of the evidence at hand."
            )
        else:
            gaps = "; ".join(missing) if missing else "none named"
            text = (
                "Interim evidence state: still incomplete "
                f"(missing: {gaps}). Search ONLY for these gaps, or "
                "answer now and name the gaps visibly in the answer."
            )
            if open_sources:
                text += f" Unread source remainders: {remainders}."
        text = HARNESS_NOTE_PREFIX + text
        return {
            "messages": [
                HumanMessage(
                    content=text,
                    additional_kwargs={SUFFICIENCY_NUDGE_FLAG: uses},
                )
            ]
        }


class KernelToolBudgetExceeded(RuntimeError):
    """The model requested a tool-call batch beyond the run limit."""

    def __init__(self, *, attempted: int, limit: int, batch_size: int) -> None:
        self.attempted = attempted
        self.limit = limit
        self.batch_size = batch_size
        super().__init__(
            f'Kernel tool budget exceeded: {attempted} requested, maximum {limit}.'
        )


def requested_tool_calls(
    messages: Any, *, uncounted: frozenset[str] = frozenset()
) -> int:
    """Count the tool calls the model requested, from checkpointed AI messages.

    THE one count behind the pre-dispatch budget and the segment-start
    rehydration, so the two can never disagree. Rejected argument batches
    count (they spent a model turn on a tool request). A call named in
    *uncounted* does not: a role's typed result rides the tool-call channel
    of the framework but executes nothing, and a role that spent its whole
    budget on reading must still be able to deliver what it found.
    """
    total = 0
    for message in messages or []:
        if isinstance(message, dict):
            role = message.get("role") or message.get("type")
            calls = [*(message.get("tool_calls") or []), *(message.get("invalid_tool_calls") or [])]
        else:
            role = getattr(message, "type", "")
            calls = [
                *(getattr(message, "tool_calls", None) or []),
                *(getattr(message, "invalid_tool_calls", None) or []),
            ]
        if role in {"ai", "assistant"}:
            total += sum(1 for call in calls if str(call.get("name") or "") not in uncounted)
    return total


class KernelToolBudgetMiddleware(AgentMiddleware):
    """Reject an overflowing model batch before any tool is dispatched.

    Counts are derived from checkpointed AI messages rather than mutable
    process state.  Re-entry and park/resume therefore produce the same total
    without double-counting, including multiple calls in one assistant turn.

    Args:
        max_tool_calls: Positive run-wide call ceiling.  The entire model
            batch is rejected when its cumulative total exceeds this value.
        uncounted_tools: Call names that execute nothing and therefore spend
            no budget (a role's typed result).
    """

    def __init__(
        self, max_tool_calls: int, *, uncounted_tools: frozenset[str] = frozenset()
    ) -> None:
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be at least 1")
        self.max_tool_calls = max_tool_calls
        self.uncounted_tools = frozenset(uncounted_tools)

    def after_model(
        self, state: dict[str, Any], runtime: Any
    ) -> dict[str, Any] | None:
        """Validate the latest model batch at the pre-dispatch boundary."""
        del runtime
        messages = state.get("messages") or []
        ai_messages = [
            message for message in messages if isinstance(message, AIMessage)
        ]
        if not ai_messages:
            return None
        latest_calls = [
            *(ai_messages[-1].tool_calls or []),
            *(ai_messages[-1].invalid_tool_calls or []),
        ]
        attempted = requested_tool_calls(ai_messages, uncounted=self.uncounted_tools)
        if attempted <= self.max_tool_calls:
            return None

        payload = {
            "attempted": attempted,
            "limit": self.max_tool_calls,
            "batch_size": len(latest_calls),
        }
        log.warning(
            "Kernel tool budget exceeded: attempted=%d limit=%d batch_size=%d; the "
            "entire batch is not executed.",
            attempted,
            self.max_tool_calls,
            len(latest_calls),
        )
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            deps = kernel_deps()
        except RuntimeError:
            deps = None
        if deps is not None:
            deps.emit(TOOL_LIMIT_REACHED_EVENT, payload)
        raise KernelToolBudgetExceeded(
            attempted=attempted,
            limit=self.max_tool_calls,
            batch_size=len(latest_calls),
        )



ACTIVITY_EVENT = "inqtrix.agent.activity"
MODEL_TURN_OPERATION = "agent.model.turn"
MODEL_TURN_ACTIVITY_ID = "model-turn"


class KernelModelTurnState(AgentState):
    kernel_usage: NotRequired[Annotated[dict[str, int], PrivateStateAttr]]


class KernelModelTurnMiddleware(AgentMiddleware):
    """Make the model boundary visible as ONE upserting activity row.

    The kernel's longest silent stretches are model turns (deep reasoning
    can run minutes without any event). ``wrap_model_call`` runs INSIDE
    the model node — it adds no graph node, so the pinned superstep price
    (``test_supersteps_per_tool_turn_is_pinned``) stays untouched; a
    ``before_model``/``after_model`` hook would each cost a superstep.
    The constant ``activity_id`` makes every turn upsert the SAME desk
    row (started -> completed) instead of stacking one row per turn.
    """

    state_schema = KernelModelTurnState

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            deps = kernel_deps()
        except RuntimeError:
            deps = None

        def emit(status: str) -> None:
            if deps is None:
                return
            deps.emit(
                ACTIVITY_EVENT,
                {
                    "activity_id": MODEL_TURN_ACTIVITY_ID,
                    "kind": "working",
                    "scope": "run",
                    "phase": "execution",
                    "operation": MODEL_TURN_OPERATION,
                    "status": status,
                },
            )

        emit("started")
        try:
            with collect_model_usage() as generated:
                response = handler(request)
            from inqtrix.agents.kernel.algorithm import _checkpointed_usage
            from types import SimpleNamespace
            from langgraph.types import Command

            prior = _checkpointed_usage(SimpleNamespace(values=request.state))
            runtime_usage = deps.run_usage() if deps is not None else {}
            receipts = {key: 0 for key in prior}
            for message in response.result:
                meta = getattr(message, "usage_metadata", None) or {}
                receipts["prompt_tokens"] += int(meta.get("input_tokens", 0))
                receipts["completion_tokens"] += int(meta.get("output_tokens", 0))
            # Both values cover overlapping calls. Runtime accounting additionally
            # includes work in other kernel nodes; never add the two totals.
            usage = {
                key: max(
                    prior[key] + max(generated[key], receipts[key]),
                    int(runtime_usage.get(key, 0)),
                )
                for key in prior
            }
            result = ExtendedModelResponse(
                model_response=response,
                command=Command(update={KERNEL_USAGE_STATE: usage}),
            )

        except Exception:
            emit("failed")
            raise
        emit("completed")
        return result

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        raise RuntimeError(
            ("KernelModelTurnMiddleware is sync-only; the kernel runs in a synchronous "
            "worker segment.")
        )


_tool_span_warned: set[str] = set()
"""Tool-span WARNINGs already logged in this process (each once)."""


def _tool_span_first_warning(key: str) -> bool:
    """``True`` the first time the tool-span WARNING *key* is due."""
    if key in _tool_span_warned:
        return False
    _tool_span_warned.add(key)
    return True


def _tool_span_safely(work: Any, *args: Any) -> Any:
    """Run span telemetry; a failure warns once and never reaches the call."""
    try:
        return work(*args)
    except Exception:  # noqa: BLE001 — telemetry never breaks a tool call
        if _tool_span_first_warning("telemetry"):
            log.warning(
                "Tool span telemetry failed; the tool call proceeds but its trace "
                "observation may be incomplete.",
                exc_info=True,
            )
        return None


def _tool_span_bubble_up_warning(exception_name: str) -> None:
    if _tool_span_first_warning("bubble_up"):
        log.warning(
            "Tool span recorded no outcome: the tool call raised %s, which is "
            "neither an interrupt nor a failure, and it passes unchanged.",
            exception_name,
        )


def _tool_span_capture_policy() -> Any:
    """The content policy when it captures content, else ``None``."""
    from inqtrix.observability import content

    policy = content.active_content_policy()
    return policy if policy.capture_content else None


def _tool_span_outcome(span: Any, outcome: str, status_message: str | None = None) -> None:
    from inqtrix.observability import semconv

    span.set_attribute(semconv.INQTRIX_OUTCOME, outcome)
    if status_message is not None:
        span.set_attribute(
            semconv.LANGFUSE_OBSERVATION_LEVEL, semconv.OBSERVATION_LEVEL_WARNING
        )
        span.set_attribute(semconv.LANGFUSE_OBSERVATION_STATUS_MESSAGE, status_message)


def _tool_span_error(span: Any, exc: Exception) -> None:
    from inqtrix.observability import semconv
    from inqtrix.observability.otel import record_projected_exception

    span.set_attribute(semconv.INQTRIX_OUTCOME, semconv.OUTCOME_ERROR)
    span.set_attribute(semconv.ERROR_TYPE, type(exc).__name__)
    record_projected_exception(span, exc)


def _executed_tool_message(result: Any, call_id: Any) -> Any:
    """The ToolMessage answering *call_id* in a tool node result, or ``None``.

    The tool node returns a ToolMessage, a Command whose mapping update
    carries the messages (dict entries stay unconverted there) or a list of
    either. Any other shape is unreadable, which the caller reports.
    """
    from collections.abc import Mapping

    from langchain_core.messages import ToolMessage, convert_to_messages
    from langgraph.types import Command

    if isinstance(result, ToolMessage):
        return result if result.tool_call_id == call_id else None
    if isinstance(result, Command):
        update = result.update
        if not isinstance(update, Mapping):
            return None
        entries = update.get("messages")
        if not isinstance(entries, (list, tuple)):
            return None
        for message in convert_to_messages(entries):
            if isinstance(message, ToolMessage) and message.tool_call_id == call_id:
                return message
        return None
    if isinstance(result, list):
        for item in result:
            message = _executed_tool_message(item, call_id)
            if message is not None:
                return message
    return None


def _tool_span_result(span: Any, result: Any, call_id: Any, policy: Any) -> None:
    """Classify an executed call's result and record its output."""
    from inqtrix.agents.kernel.tools import SOURCE_TOOL_FAILURE_PREFIXES
    from inqtrix.observability import content, semconv

    message = _executed_tool_message(result, call_id)
    if message is None:
        span.set_attribute(semconv.INQTRIX_TOOL_RESULT_UNREADABLE, True)
        if _tool_span_first_warning("result_unreadable"):
            log.warning(
                "Tool span could not read a ToolMessage for its call in the tool "
                "result; the trace observation records no outcome (%s).",
                semconv.INQTRIX_TOOL_RESULT_UNREADABLE,
            )
        return
    # ``text`` reads a str content and the text blocks of a block list alike.
    text = message.text
    prefix = next(
        (item for item in SOURCE_TOOL_FAILURE_PREFIXES if text.startswith(item)),
        None,
    )
    if prefix is not None:
        _tool_span_outcome(span, semconv.OUTCOME_FAILED, prefix)
    elif message.status == "error":
        _tool_span_outcome(
            span, semconv.OUTCOME_FAILED, semconv.STATUS_MESSAGE_ERROR_STATUS
        )
    else:
        _tool_span_outcome(span, semconv.OUTCOME_COMPLETED)
    if policy is not None:
        _tool_span_safely(
            content.record_content,
            span,
            semconv.LANGFUSE_OBSERVATION_OUTPUT,
            message.content,
            policy,
        )


class KernelToolSpanMiddleware(AgentMiddleware):
    """One trace observation per tool call the kernel's tool node executes.

    Each executed call gets an ``execute_tool <name>`` span (Langfuse type
    tool) carrying the call id, so its timeline row finds it. The span is
    the first kernel middleware and so wraps every layer that can change a
    result; HITL edits happen before dispatch, so it records the executed
    call. Only ``wrap_tool_call`` is overridden: a ``before_*``/``after_*``
    hook would add a graph node and re-price the pinned supersteps.

    The span never changes the call: the result object and any exception
    leave unchanged, and a telemetry failure warns once per process instead
    of failing the tool. Arguments and result text reach the span only
    through the content policy, read only behind a recording span. The SDK's
    exception recording is off; an unexpected exception is recorded
    projected (type, stable code, frame locations), never with its text.

    Outcomes: ``completed``; ``failed`` at WARNING for a visible failure (a
    :data:`~inqtrix.agents.kernel.tools.SOURCE_TOOL_FAILURE_PREFIXES` text
    or an error-status ToolMessage); ``parked`` for an interrupt (the
    resumed execution is a second span with the same call id);
    ``cancelled``; ``budget_exceeded`` at WARNING; ``error`` with ERROR
    status for any other exception.
    """

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        from langgraph.errors import GraphBubbleUp, GraphInterrupt

        from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded
        from inqtrix.observability import content, semconv
        from inqtrix.observability.otel import operation_span

        call = request.tool_call
        registered = request.tool is not None
        # A name the model invented is model output: it stays out of the
        # span name and attributes, and reaches the trace only as captured
        # input.
        name = str(call.get("name") or "") if registered else semconv.UNKNOWN_TOOL_NAME
        call_id = call.get("id")
        attributes: dict[str, Any] = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OPERATION_EXECUTE_TOOL,
            semconv.GEN_AI_TOOL_NAME: name,
            semconv.GEN_AI_TOOL_TYPE: semconv.TOOL_TYPE_FUNCTION,
        }
        if call_id:
            attributes[semconv.GEN_AI_TOOL_CALL_ID] = str(call_id)
        with operation_span(
            semconv.execute_tool_span_name(name),
            attributes,
            record_exceptions=False,
        ) as span:
            if span is None or not span.is_recording():
                return handler(request)
            policy = _tool_span_safely(_tool_span_capture_policy)
            if policy is not None:
                _tool_span_safely(
                    content.record_content,
                    span,
                    semconv.LANGFUSE_OBSERVATION_INPUT,
                    call.get("args")
                    if registered
                    else {"name": call.get("name"), "args": call.get("args")},
                    policy,
                )
            try:
                result = handler(request)
            except GraphInterrupt:
                _tool_span_safely(_tool_span_outcome, span, semconv.OUTCOME_PARKED)
                raise
            except GraphBubbleUp as exc:
                _tool_span_safely(_tool_span_bubble_up_warning, type(exc).__name__)
                raise
            except AgentTokenBudgetExceeded:
                _tool_span_safely(
                    _tool_span_outcome,
                    span,
                    semconv.OUTCOME_BUDGET_EXCEEDED,
                    semconv.STATUS_MESSAGE_TOKEN_BUDGET_EXCEEDED,
                )
                raise
            except AgentCancelled:
                _tool_span_safely(_tool_span_outcome, span, semconv.OUTCOME_CANCELLED)
                raise
            except Exception as exc:
                _tool_span_safely(_tool_span_error, span, exc)
                raise
            _tool_span_safely(_tool_span_result, span, result, call_id, policy)
            return result

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        raise RuntimeError(
            "KernelToolSpanMiddleware is sync-only; the kernel runs in a synchronous "
            "worker segment."
        )


CONTEXT_COMPACTED_EVENT = "inqtrix.agent.context.compacted"

KERNEL_SUMMARY_PROMPT = (
    "Summarise the following older conversation of a research agent "
    "compactly. MANDATORY: carry over every reference_id value (ref_...), "
    "every evidence label ([K1], [W2], ...), every artifact_id value and "
    "every open task VERBATIM — they are part of the contract, not prose. "
    "Summarise tool outputs by their information content, not by their "
    "wording.\n\n"
    + untrusted_fence("{messages}", "older-conversation")
)
"""Kernel summary prompt: citation tokens survive compaction verbatim.

The evidence ledger itself is persisted separately (``evidence_bundle``
artifact, rehydrated on resume), so compaction can never destroy the
citation TRUTH — this prompt additionally keeps the citation TOKENS in
the summarized transcript so the model keeps citing correctly.

The conversation it condenses is fenced as data: it holds tool outputs
and source text, and the SDK serializes it as XML, which escapes the
fences those outputs carried — without this fence the summary model
would read source text on the same footing as its instructions. The
serialization escapes ``<`` inside every message, so nothing in the
conversation can close this fence."""

current_compaction_todos: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "inqtrix_kernel_compaction_todos", default=None
)
"""The write_todos state of the segment currently inside wrap_model_call.

A ContextVar (not middleware instance state): the compiled graph — and
thus the middleware instance — is shared across concurrent runs, while
the todo recitation must come from THE run being compacted."""

current_compaction_assignment: contextvars.ContextVar[str] = contextvars.ContextVar(
    "inqtrix_kernel_compaction_assignment", default=""
)
"""The assignment of the run currently inside wrap_model_call, verbatim.

Same reason for a ContextVar as the todos. A compaction condenses
everything before its cutoff, the run's first messages included, and the
summary goes back fenced as data — so the assignment travels next to it,
verbatim and outside the fence, or compaction would turn the run's
instructions into data."""

SUMMARY_CHARS_KEY = "inqtrix_summary_chars"
SUMMARY_FAILED_KEY = "inqtrix_summary_failed"
"""Facts about the condensed summary itself, set on the summary message.

The message carries more than the summary (the assignment, the archive
pointer, the todo state), so its length is not the summary's length, and
an assignment quoting the SDK's error text must not read as a failed
summary."""


def run_assignment(messages: Any) -> str:
    """The run's assignment: its thread's leading messages, verbatim.

    Everything before the first model turn — the first user message and
    the skill block the skill middleware adds before the first call. Read
    from the thread state, which compaction never mutates, so every
    compaction of a run finds the same assignment, never a prior summary.
    """
    lead: list[str] = []
    for message in messages or []:
        if getattr(message, "type", "") == "ai":
            break
        if getattr(message, "type", "") == "human":
            text = message_text(message).strip()
            if text:
                lead.append(text)
    return "\n\n".join(lead)


def render_todo_state(todos: Any) -> str:
    """Render the write_todos state for recitation inside a summary."""
    if not isinstance(todos, list) or not todos:
        return ""
    lines = []
    for todo in todos:
        if not isinstance(todo, dict):
            continue
        status = str(todo.get("status") or "pending")
        content = str(todo.get("content") or "").strip()
        if content:
            lines.append(f"- [{status}] {content}")
    return "\n".join(lines)


def message_text(message: Any) -> str:
    """Plain text of a LangChain message (string or content blocks)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


def emit_compaction_event(before: Any, after: dict[str, Any]) -> None:
    """Emit the visible compaction event + narration (No Silent Fallbacks).

    Called by the harness summarization middleware after a compaction
    landed. ``before``/``after`` are ``_summarization_event`` dicts; the
    narration id is a content hash of the summary, so replayed segments
    emit the identical id (replay-safe events).
    """
    from inqtrix.agents.kernel.deps import kernel_deps

    try:
        deps = kernel_deps()
    except RuntimeError:
        return
    prior_cutoff = (
        int(before.get("cutoff_index") or 0) if isinstance(before, dict) else 0
    )
    cutoff = int(after.get("cutoff_index") or 0)
    summary_message = after.get("summary_message")
    summary_text = message_text(summary_message)
    facts = getattr(summary_message, "additional_kwargs", None) or {}
    archive_id = str(after.get("file_path") or "")
    # deepagents swallows summary-model failures into a literal error
    # string used AS the summary — a compaction that lost its summary
    # must not report success (No Silent Fallbacks); the archive pointer
    # keeps the evicted history recoverable either way. Read from the
    # summary itself, never from the message around it.
    summary_failed = bool(facts[SUMMARY_FAILED_KEY])
    payload = {
        "archive_artifact_id": archive_id,
        "messages_summarized": max(0, cutoff - prior_cutoff),
        "summary_chars": int(facts[SUMMARY_CHARS_KEY]),
        "trigger_tokens": deps.context_trigger_tokens,
        "summary_failed": summary_failed,
    }
    if summary_failed:
        log.warning(
            "Kernel context compacted WITHOUT a summary after a provider error in the "
            "summary call; archive %s is complete.",
            archive_id or "-",
        )
    else:
        log.info(
            "Kernel context compacted: %d messages summarized (archive %s, trigger %d "
            "tokens).",
            payload["messages_summarized"],
            archive_id or "-",
            deps.context_trigger_tokens,
        )
    deps.emit(CONTEXT_COMPACTED_EVENT, payload)
    digest = hashlib.sha1(summary_text.encode("utf-8")).hexdigest()[:8]
    # The narration is the USER surface of No-Silent-Fallbacks: a
    # compaction that lost its summary must say so here too, not only
    # in the structured event flag.
    narration_text = t(
        {"language": resolve_answer_language(deps.question).code},
        "kernel_context_compacted_without_summary"
        if summary_failed
        else "kernel_context_compacted",
    )
    # Content-hash id: a replayed segment emits the identical narration
    # id (replay-safe events).
    emit_narration(
        deps.emit,
        narration_id=f"ctx_{digest}",
        kind="status",
        text=narration_text,
        phase="execution",
        guard=deps.guard_reader_text,
    )


CHILD_TOOL_NAMES = frozenset(
    {"run_web_research", "run_deep_mission", "delegate_batch"}
)
"""Kernel tools that submit child runs and interrupt inside their body."""

CHILD_BATCH_GUARDED_EVENT = "inqtrix.agent.child_batch.guarded"


INTERRUPTING_TOOL_NAMES: frozenset[str] = frozenset({"ask_user"})
"""Non-child tools whose BODY interrupts the segment (ask_user parks
``waiting_for_input``). At most one interrupt source may dispatch per
model turn: a child submission next to an ask_user call would trip the
multi-interrupt backstop AFTER the submission — an orphaned child
burning quota behind a crashed parent."""


def turn_has_interrupting_call(last_ai: Any) -> bool:
    """Whether the turn carries an ``ask_user``-class interrupting call."""
    return any(
        call.get("name") in INTERRUPTING_TOOL_NAMES
        for call in (getattr(last_ai, "tool_calls", None) or [])
    )


def last_turn_child_calls(
    messages: list[Any],
) -> tuple[Any | None, list[dict[str, Any]]]:
    """The last AIMessage and its child-tool calls (the current turn).

    THE shared reading of "how many child dispatches does this turn
    carry" — the batch guard voids multi-child turns with it, and the
    gate policy's ``when`` predicate skips HITL for exactly those turns
    (one authority per call: gating a doomed batch would park an
    approval whose actions never execute, and a reject would answer the
    calls twice — once by HITL, once by the guard).
    """
    last_ai = next(
        (
            message
            for message in reversed(messages or [])
            if isinstance(message, AIMessage)
        ),
        None,
    )
    if last_ai is None:
        return None, []
    return last_ai, [
        call
        for call in (last_ai.tool_calls or [])
        if call["name"] in CHILD_TOOL_NAMES
    ]


class KernelChildBatchGuardMiddleware(AgentMiddleware):
    """More than one child-tool call per model turn never reaches dispatch.

    Each child tool interrupts individually inside its body; two of them
    dispatched in one segment would trip the multi-interrupt guard AFTER
    both submissions — orphaned children burning quota behind a crashed
    parent. This guard answers every child call of the offending batch
    with an artificial ToolMessage pointing at ``delegate_batch`` and
    strips them from the AIMessage (the HITL middleware's own rewrite
    mechanics) — PRE-dispatch, so nothing was submitted. The algorithm's
    multi-interrupt RuntimeError stays as the loud backstop for unknown
    future interrupt sources.

    The gate policy's ``when`` predicate keeps HITL away from these
    turns (see :func:`last_turn_child_calls`), so the guard is the ONE
    answering authority; the already-answered skip below stays as the
    structural double-answer backstop should middleware ordering ever
    change (a second ToolMessage per call id corrupts the transcript
    and providers reject the next model call).
    """

    def after_model(
        self, state: dict[str, Any], runtime: Any
    ) -> dict[str, Any] | None:
        del runtime
        messages = state.get("messages") or []
        last_ai, child_calls = last_turn_child_calls(messages)
        if last_ai is None or not child_calls:
            return None
        # ask_user + child in ONE turn is the same orphan trap as two
        # children: the child submits, then BOTH bodies interrupt. The
        # clarification wins; the child is voided pre-dispatch.
        ask_turn = turn_has_interrupting_call(last_ai)
        if len(child_calls) <= 1 and not ask_turn:
            return None
        answered: set[str] = set()
        for message in reversed(messages):
            if message is last_ai:
                break
            if getattr(message, "type", "") == "tool":
                answered.add(
                    str(getattr(message, "tool_call_id", "") or "")
                )
        open_calls = [
            call for call in child_calls if call["id"] not in answered
        ]
        if not open_calls:
            return None
        from langchain_core.messages import ToolMessage

        # The calls STAY on the AIMessage — an artificial ToolMessage per
        # call answers them (the tool node skips answered calls), exactly
        # the HITL middleware's reject mechanics. Removing them would
        # leave orphan ToolMessages the next model call rejects.
        corrective_text = (
            (
                "A clarifying question and a subtask do not fit into ONE "
                "turn: ask_user runs now; start the subtask only in the "
                "next turn, once the answer is available."
            )
            if ask_turn
            else (
                "Several subtask tools in ONE turn are not allowed. Use "
                "delegate_batch with up to 3 assignments for parallel "
                "subtasks — or call the single tools one after another."
            )
        )
        corrective = [
            ToolMessage(
                content=corrective_text,
                name=call["name"],
                tool_call_id=call["id"],
                status="error",
            )
            for call in open_calls
        ]
        payload = {
            "batch_size": len(child_calls),
            "tools": sorted({call["name"] for call in child_calls}),
        }
        log.warning(
            "Kernel child batch guard intercepted %d parallel child tools in one turn "
            "(%s); nothing was submitted.",
            payload["batch_size"],
            ", ".join(payload["tools"]),
        )
        from inqtrix.agents.kernel.deps import kernel_deps

        try:
            deps = kernel_deps()
        except RuntimeError:
            deps = None
        if deps is not None:
            deps.emit(CHILD_BATCH_GUARDED_EVENT, payload)
            # Visible interception (No Silent Fallbacks): a guarded batch
            # flows through the same narration channel as compaction, so
            # the user sees WHY nothing was dispatched. Content-hash id
            # over the answered call ids keeps a replayed segment stable.
            digest = hashlib.sha1(
                ",".join(sorted(call["id"] for call in open_calls)).encode(
                    "utf-8"
                )
            ).hexdigest()[:8]
            emit_narration(
                deps.emit,
                narration_id=f"batchguard_{digest}",
                kind="status",
                text=t(
                    {
                        "language": resolve_answer_language(
                            deps.question
                        ).code
                    },
                    "kernel_batchguard_ask_first"
                    if ask_turn
                    else "kernel_batchguard_bundled",
                ),
                phase="execution",
                guard=deps.guard_reader_text,
            )
        return {"messages": [last_ai, *corrective]}
