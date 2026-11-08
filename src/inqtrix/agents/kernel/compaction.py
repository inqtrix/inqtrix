"""The kernel's own context compaction, on public LangChain hooks.

A compaction never rewrites the thread. The state keeps every message; the
model call sees a computed view: one summary of the older history, then the
newest messages. The record of the latest compaction is the only state it
writes (``inqtrix_compaction``), and the summary message is rendered from it
once, so the prompt-cache prefix stays the same until the next compaction.

Before any summary, tool results the model has already answered give way to
placeholders in the view once it reaches the clearing threshold (stage one):
each placeholder names how to read the original again and the evidence it
listed. It costs no model call and changes no state. Only when the view still
reaches the trigger after that does a summary follow (stage two). The
thresholds follow the LangChain and Anthropic defaults for clearing tool
results.

The order of one compaction is fixed, in both hooks: the summary request is
checked against the model's input budget, the evicted history goes to the run
archive verbatim, the summary call condenses it as the view showed it,
together with the prior summary, the model call runs on the new view, and
only then the visible event and the state update follow. A failure anywhere
before the model returns leaves no event and no record behind; the next
attempt is a new paid summary call. The archive is content-addressed, but a
kernel section's identity also carries its transcript descriptor, which holds
run-wide data such as the source receipts: when that data changed in between,
the retry writes the same text as a second section.

The sync hook serves the kernel's segment runner (``graph.stream``), the
async hook the same steps for an async runner; both share the pure steps
below and differ only in how they wait. The kernel runs synchronously, and
its other middlewares refuse an async run. The async hook does not reach the
archive from the process loop yet: it hands the archive write to
``asyncio.to_thread``, the short-work lane, whose threads may not wait for the
loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, NamedTuple, NotRequired, TypedDict

from langchain.agents.middleware import AgentMiddleware, AgentState, ExtendedModelResponse
from langchain.agents.middleware.types import OmitFromSchema
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, get_buffer_string
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command

from inqtrix.agents.context_archive import archived_result_id
from inqtrix.agents.context_sources import SOURCE_COVERAGE, coverage_records_from_artifact
from inqtrix.agents.kernel.deps import kernel_deps
from inqtrix.agents.kernel.middleware import (
    KERNEL_SUMMARY_PROMPT,
    message_text,
    render_todo_state,
    run_assignment,
)
from inqtrix.agents.narration import emit_narration
from inqtrix.agents.telemetry import COMPACTION_PURPOSE
from inqtrix.citation_labels import DELEGATED_FAMILIES, citation_pattern
from inqtrix.exceptions import (
    AgentContextArchiveError,
    AgentModelCapacityError,
    AgentStructuredOutputError,
    ModelCapacityReason,
)
from inqtrix.harness.contracts.events import CONTEXT_COMPACTED_EVENT
from inqtrix.i18n import t
from inqtrix.observability.context import bind_purpose
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.providers.base import INCOMPLETE_FINISH_REASONS
from inqtrix.untrusted_content import (
    UNTRUSTED_FENCE_CLOSE,
    UNTRUSTED_FENCE_OPEN,
    neutralize_fence_delimiters,
)

log = logging.getLogger(__name__)

COMPACTION_STATE = "inqtrix_compaction"
"""State key of the latest compaction record; part of the workflow fingerprint."""

COMPACTION_REGROWTH_FRACTION = 0.1
"""How far, as a share of the trigger, the view must grow past the size a
compaction left, or past a compaction that did not shrink it, before the
next one is tried. Without it a large kept tail would buy a paid summary on
every turn."""

COMPACTION_SUMMARY_FLAG = "inqtrix_compaction_generation"
"""Marks the summary message in the view with its generation."""

CLEAR_TRIGGER_TOKENS = 100_000
"""View size from which stage one clears tool results, unless the compaction
trigger lies lower."""

CLEAR_KEEP = 3
"""Newest tool results that stage one always leaves verbatim."""

NEVER_CLEARED_TOOLS = frozenset({"ask_user", "load_skill"})
"""Results that carry the user's words or instructions still in force."""

CLEARED_RESULT_FLAG = "inqtrix_cleared_result"
"""Marks a tool result whose text a view replaced with a placeholder."""

_FENCED = re.compile(
    re.escape(UNTRUSTED_FENCE_OPEN) + r"\b[^>]*>.*?" + re.escape(UNTRUSTED_FENCE_CLOSE), re.DOTALL
)
"""Fenced source content: a label quoted there is the source's, not the tool's."""

_REFERENCE_ID = re.compile(r"\bref_[0-9a-f]{16}\b")
"""The opaque id of an evidence record, as the tools list it."""


class CompactionRecord(TypedDict):
    """The latest compaction of a run.

    Attributes:
        cutoff: Index of the first state message the view keeps.
        summary: Text of the latest summary; the next compaction folds it.
        message: Rendered content of the summary message the view starts with.
        archive_refs: Archive sections of every compaction so far, oldest first.
        generation: Number of compactions so far.
        size_after: Tokens of the view right after this compaction.
    """

    cutoff: int
    summary: str
    message: str
    archive_refs: list[str]
    generation: int
    size_after: int


class CompactionState(AgentState):
    """The compaction record, kept out of the graph's input and output."""

    inqtrix_compaction: NotRequired[
        Annotated[CompactionRecord | None, OmitFromSchema(input=True, output=True)]
    ]


@dataclass(frozen=True)
class CompactionPlan:
    """What the next model call sees, and whether it compacts first.

    Attributes:
        view: The messages the model sees without a new compaction.
        tokens: Tokens of that view, counted like the trigger.
        compact: Whether this call compacts.
        stalled: The view crossed the trigger, but nothing new can be evicted.
        cutoff: Index of the first state message the new view keeps.
        evicted: State messages a compaction now moves into the archive, verbatim.
        summarized: The same messages as the view showed them; the summary
            condenses these.
    """

    view: list[Any]
    tokens: int
    compact: bool
    stalled: bool = False
    cutoff: int = 0
    evicted: tuple[Any, ...] = ()
    summarized: tuple[Any, ...] = ()


def count_view_tokens(messages: list[Any], system_message: Any = None, tools: Any = None) -> int:
    """Approximate input tokens of one model call: tool schemas, system, messages.

    The one count for the compaction trigger, its shrink check and the input
    budget of the model call, so the three never disagree near a boundary.
    """
    from langchain_core.utils.function_calling import convert_to_openai_tool

    schemas = [convert_to_openai_tool(tool) for tool in tools or []]
    overhead = count_tokens_approximately(
        [SystemMessage(content=json.dumps(schemas, ensure_ascii=False))]
        + ([system_message] if system_message else [])
    )
    return overhead + count_tokens_approximately(messages)


def view_counter(system_message: Any = None, tools: Any = None) -> Callable[[list[Any]], int]:
    """:func:`count_view_tokens` for many views of one call; the schemas count once."""
    overhead = count_view_tokens([], system_message, tools)
    return lambda messages: overhead + count_tokens_approximately(messages)


def safe_cutoff(messages: list[Any], keep: int) -> int:
    """Index from which the newest ``keep`` messages stay, never splitting a tool call.

    A cutoff on a tool result moves back to the model turn that asked for it,
    so call and results stay together; without that turn it moves past the
    results. Returns 0 when there is nothing to cut.
    """
    if len(messages) <= keep:
        return 0
    cutoff = len(messages) - keep
    if not isinstance(messages[cutoff], ToolMessage):
        return cutoff
    call_ids: set[str] = set()
    index = cutoff
    while index < len(messages) and isinstance(messages[index], ToolMessage):
        if messages[index].tool_call_id:
            call_ids.add(messages[index].tool_call_id)
        index += 1
    for position in range(cutoff - 1, -1, -1):
        message = messages[position]
        if isinstance(message, AIMessage) and message.tool_calls:
            if call_ids & {call.get("id") for call in message.tool_calls if call.get("id")}:
                return position
    return index


def compacted_view(messages: list[Any], record: CompactionRecord | None) -> list[Any]:
    """The messages a model call sees after the latest compaction."""
    if not record:
        return list(messages)
    summary = HumanMessage(
        content=record["message"],
        additional_kwargs={COMPACTION_SUMMARY_FLAG: record["generation"]},
    )
    return [summary, *messages[record["cutoff"]:]]


def tool_calls_by_id(messages: list[Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    """Name and arguments of every tool call in *messages*, by call id."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                if call.get("id"):
                    calls[call["id"]] = (str(call.get("name") or ""), dict(call.get("args") or {}))
    return calls


class Reread(NamedTuple):
    """How the model reads a cleared tool result again: the tool, and the call."""

    tool: str
    hint: str


@dataclass(frozen=True)
class RereadScope:
    """What a placeholder may point to in one model call.

    Attributes:
        archive_prefix: The run archive whose parts a placeholder may name.
        tools: The tools the call offers; a placeholder names only one of them.
    """

    archive_prefix: str
    tools: frozenset[str]


def tool_names(tools: Any) -> frozenset[str]:
    """Names of the tools a model call offers, in either form a request holds them."""
    names = set()
    for tool in tools or []:
        if isinstance(tool, dict):
            names.add(str(tool.get("name") or (tool.get("function") or {}).get("name") or ""))
        else:
            names.add(str(getattr(tool, "name", "") or ""))
    return frozenset(names - {""})


def archive_reread(artifact_id: str) -> Reread:
    """The way back to a tool result the run archive keeps in full."""
    return Reread("read_canvas", f"Full text: read_canvas(artifact_id='{artifact_id}').")


def unwritten_section(prefix: str) -> str:
    """A section id as long as the archive's own, to size a pointer before anything is written."""
    return f"{prefix}_{'0' * 20}"


def rereadable_original(
    message: Any, calls: dict[str, tuple[str, dict[str, Any]]], scope: RereadScope
) -> Reread | None:
    """How the model reads a tool result again, or ``None`` when it cannot.

    Two kinds of results can be read again: a result the run archive keeps
    in full, which names its archive part in its own last line, and a window
    of a source whose every coverage record names the version it read, which
    the same call reads again. Either way the reading tool must be one this
    call offers.
    """
    artifact_id = archived_result_id(message_text(message), scope.archive_prefix)
    if artifact_id:
        return archive_reread(artifact_id) if "read_canvas" in scope.tools else None
    artifact = getattr(message, "artifact", None)
    if not isinstance(artifact, dict) or SOURCE_COVERAGE not in artifact:
        return None
    records = coverage_records_from_artifact(artifact)
    call = calls.get(getattr(message, "tool_call_id", "") or "")
    if not records or not call or not all(record.version for record in records):
        return None
    name, args = call
    if name not in scope.tools:
        return None
    arguments = neutralize_fence_delimiters(
        ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in sorted(args.items()))
    )
    versions = ", ".join(dict.fromkeys(record.version for record in records))
    return Reread(name, f"Read it again with {name}({arguments}); it read source version {versions}.")


def placeholder_for(message: Any, reread: str, *, seen: bool = True) -> Any:
    """A cleared tool result: how to read it again, and the evidence it listed.

    A result the model has not seen yet names no evidence, so nothing of it is
    cited before it is read.
    """
    name = message.name or "a tool"
    if seen:
        listed = _FENCED.sub(" ", message_text(message))
        labels = list(dict.fromkeys(citation_pattern(DELEGATED_FAMILIES).findall(listed)))
        references = list(dict.fromkeys(_REFERENCE_ID.findall(listed)))
        text = f"[Result of {name} cleared from this view to save input. {reread}"
        if labels:
            text += " Evidence labels in it: " + " ".join(f"[{label}]" for label in labels) + "."
        if references:
            text += " Reference ids in it: " + ", ".join(references) + "."
    else:
        text = f"[Result of {name} not shown: the input would not fit with it, and you have not seen it yet. {reread}"
    return message.model_copy(
        update={
            "content": text + "]",
            "additional_kwargs": {**message.additional_kwargs, CLEARED_RESULT_FLAG: True},
        }
    )


def clearing_threshold(trigger: int) -> int:
    """View size from which stage one clears; 0, like the trigger, disables it."""
    return min(CLEAR_TRIGGER_TOKENS, trigger) if trigger > 0 else 0


def clear_tool_results(
    view: list[Any],
    calls: dict[str, tuple[str, dict[str, Any]]],
    *,
    threshold: int,
    count: Callable[[list[Any]], int],
    scope: RereadScope,
    keep: int = CLEAR_KEEP,
) -> list[Any]:
    """Stage one: tool results the model has answered give way to placeholders.

    Below *threshold*, counted on the view before clearing, the view stays
    as it is. From it on every result gives way except the newest *keep*
    results, results the model has not answered yet, results that carry the
    user's words or instructions in force, and results that cannot be read
    again. The view before clearing only grows until the next summary, so
    the cleared set only grows too: each turn changes the view only where a
    result leaves the newest few, and the cached prompt up to there holds.
    """
    if threshold <= 0 or count(view) < threshold:
        return view
    answered = max((index for index, message in enumerate(view) if isinstance(message, AIMessage)), default=-1)
    results = [
        index
        for index, message in enumerate(view)
        if isinstance(message, ToolMessage)
        and message.name not in NEVER_CLEARED_TOOLS
        and not message.additional_kwargs.get(CLEARED_RESULT_FLAG)
    ]
    newest = set(results[-keep:]) if keep > 0 else set()
    cleared = list(view)
    for index in results:
        if index > answered or index in newest:
            continue
        reread = rereadable_original(view[index], calls, scope)
        if reread:
            cleared[index] = placeholder_for(view[index], reread.hint)
    return cleared


def visible_view(
    messages: list[Any],
    record: CompactionRecord | None,
    *,
    trigger: int,
    count: Callable[[list[Any]], int],
    scope: RereadScope,
) -> list[Any]:
    """What a model call sees: the latest compaction's view after stage one."""
    return clear_tool_results(
        compacted_view(messages, record),
        tool_calls_by_id(messages),
        threshold=clearing_threshold(trigger),
        count=count,
        scope=scope,
    )


def plan_compaction(
    messages: list[Any],
    record: CompactionRecord | None,
    *,
    trigger: int,
    keep: int,
    scope: RereadScope,
    hold: int = 0,
    count: Callable[[list[Any]], int] = count_view_tokens,
) -> CompactionPlan:
    """Decide whether the next model call compacts, and what it would evict.

    Args:
        messages: The run's state messages, never rewritten.
        record: The latest compaction, or ``None``.
        trigger: Tokens of the view at which a compaction starts; 0 disables
            both stages.
        keep: Newest messages a compaction keeps verbatim.
        scope: What a placeholder of stage one may point to.
        hold: Tokens below which this segment does not try again after a
            compaction that did not shrink the view.
        count: Token count of a view, the trigger's count.
    """
    view = visible_view(messages, record, trigger=trigger, count=count, scope=scope)
    tokens = count(view)
    if trigger <= 0:
        return CompactionPlan(view=view, tokens=tokens, compact=False)
    threshold = max(trigger, hold)
    if record:
        threshold = max(threshold, record["size_after"] + regrowth(trigger))
    if tokens < threshold:
        return CompactionPlan(view=view, tokens=tokens, compact=False)
    prior = record["cutoff"] if record else 0
    cutoff = max(prior, safe_cutoff(messages, keep))
    if cutoff <= prior:
        # Only the kept part is left above the trigger: a summary would
        # condense nothing new and lose the archive pointer.
        return CompactionPlan(view=view, tokens=tokens, compact=False, stalled=True)
    start = 1 if record else 0
    return CompactionPlan(
        view=view,
        tokens=tokens,
        compact=True,
        cutoff=cutoff,
        evicted=tuple(messages[prior:cutoff]),
        summarized=tuple(view[start : start + cutoff - prior]),
    )


def regrowth(trigger: int) -> int:
    """Tokens a view must grow by before another compaction is tried."""
    return max(1, int(trigger * COMPACTION_REGROWTH_FRACTION))


def summary_request(prior: str, evicted: tuple[Any, ...]) -> str:
    """The summary prompt: the prior summary folded with the newly evicted history."""
    conversation = list(evicted)
    if prior:
        conversation.insert(
            0, HumanMessage(content="Summary of the conversation before this part:\n" + prior)
        )
    return KERNEL_SUMMARY_PROMPT.format(
        messages=get_buffer_string(conversation, format="xml")
    ).rstrip()


def loaded_skills(messages: list[Any]) -> list[str]:
    """The instructions of every skill the model loaded, verbatim, oldest first.

    A refused ``load_skill`` call loaded nothing; restated as an instruction
    that still applies, it would contradict the run's state.
    """
    from inqtrix.agents.kernel.tools import loaded_skill_result

    return [message_text(message) for message in messages if loaded_skill_result(message)]


def render_summary_message(
    *,
    assignment: str,
    question: str,
    answers: list[str],
    summary: str,
    archive_refs: list[str],
    archive_prefix: str,
    todos: Any,
    skills: list[str],
) -> str:
    """The summary message: instructions outside the fence, the summary inside.

    A compaction must not change what is instruction and what is data. The
    summary condenses tool outputs, so it goes back fenced. The assignment,
    the user's answers and the instructions of loaded skills travel next to
    it, verbatim and outside the fence, read from the run, never from the
    summary.
    """
    from inqtrix.untrusted_content import untrusted_fence

    parts: list[str] = []
    if assignment:
        parts.append(
            "The assignment of this run, verbatim as it was given "
            "(it still directs your work):\n" + assignment
        )
    else:
        log.warning(
            "Kernel compaction without the run's assignment in reach; "
            "only the question is restated outside the summary."
        )
        if question:
            parts.append(f"Your task (restated from the run):\n{question}")
    if answers:
        parts.append("The user's answers to your questions in this run:\n" + "\n\n".join(answers))
    if skills:
        parts.append(
            "Instructions of the skills you loaded in this run, verbatim (they still apply):\n"
            + "\n\n".join(skills)
        )
    parts.append(
        "Summary of the conversation so far (a condensation of earlier messages "
        "and tool outputs):\n" + untrusted_fence(summary, "conversation-summary")
    )
    listed = "; ".join(f"read_canvas(artifact_id='{ref}')" for ref in archive_refs)
    parts.append(
        (
            f"The older conversation is stored as a section in the run archive: {listed}."
            if len(archive_refs) == 1
            else f"The older conversation is stored in {len(archive_refs)} sections in the "
            f"run archive: {listed}."
        )
        + f" All archive sections: read_canvas(artifact_id='{archive_prefix}')."
    )
    todos_text = render_todo_state(todos)
    if todos_text:
        parts.append(f"Current task state:\n{todos_text}")
    return "\n\n".join(parts)


def warn_if_the_assignment_crowds_the_trigger(assignment: str, trigger: int) -> None:
    """The assignment travels uncondensed; say when it takes half the trigger."""
    if trigger <= 0 or not assignment:
        return
    tokens = count_tokens_approximately([HumanMessage(content=assignment)])
    if tokens * 2 > trigger:
        log.warning(
            "The run's assignment (~%d tokens) takes more than half of the compaction "
            "trigger (%d tokens); it stays verbatim, so the run may compact on almost "
            "every turn.",
            tokens,
            trigger,
        )


def narration_id(generation: int, archive_refs: list[str]) -> str:
    """Stable id of the compaction's narration: its generation and first new section.

    A retry after a failure rebuilds the same state, so it reaches the same
    generation and, while the run-wide data in the transcript descriptor is
    unchanged, the same section; the desk then replaces the line in place
    instead of showing it twice.
    """
    first = archive_refs[0] if archive_refs else ""
    return f"ctx_{generation}_{hashlib.sha1(first.encode('utf-8')).hexdigest()[:8]}"


def _summary_text(reply: Any, model: str) -> str:
    """The summary from the model's reply, or a loud failure."""
    finish_reason = str((getattr(reply, "response_metadata", None) or {}).get("finish_reason") or "")
    if finish_reason in INCOMPLETE_FINISH_REASONS:
        if finish_reason == "content_filter":
            raise AgentStructuredOutputError(
                model, "context_compaction", "The provider filtered the compaction summary."
            )
        raise AgentModelCapacityError(
            model,
            "context_compaction",
            f"The compaction summary was cut off ({finish_reason}); nothing was compacted.",
            reason=ModelCapacityReason.OUTPUT_LIMIT,
        )
    text = str(getattr(reply, "text", "") or "").strip()
    if not text or getattr(reply, "tool_calls", None):
        raise AgentStructuredOutputError(
            model, "context_compaction", "The compaction summary returned no usable text."
        )
    return text


class InqtrixCompactionMiddleware(AgentMiddleware):
    """The kernel's one compaction: archived, folded, visible and loud.

    The trigger comes per run from ``kernel_deps().context_trigger_tokens``
    (the compiled graph is shared across runs and models; 0 disables it), and
    so do the hold of a segment whose last compaction did not shrink the view
    and the one warning for a view that only its kept part fills.
    """

    state_schema = CompactionState

    def __init__(self, *, keep_messages: int) -> None:
        super().__init__()
        self.keep_messages = int(keep_messages)

    @staticmethod
    def _scope(request: Any, deps: Any) -> RereadScope:
        return RereadScope(deps.context_archive_prefix, tool_names(request.tools))

    def _plan(self, request: Any, deps: Any) -> CompactionPlan:
        return plan_compaction(
            list(request.messages),
            request.state.get(COMPACTION_STATE),
            trigger=int(deps.context_trigger_tokens or 0),
            keep=self.keep_messages,
            scope=self._scope(request, deps),
            hold=int(deps.compaction_hold_tokens or 0),
            count=view_counter(request.system_message, request.tools),
        )

    @staticmethod
    def _deps() -> Any | None:
        try:
            return kernel_deps()
        except RuntimeError:
            return None

    @staticmethod
    def _warn_stalled(deps: Any, plan: CompactionPlan) -> None:
        if deps.compaction_stall_warned:
            return
        deps.compaction_stall_warned = True
        log.warning(
            "Kernel compaction skipped: only the newest messages are left above the trigger "
            "(%d tokens of view, trigger %d); nothing older is left to condense.",
            plan.tokens,
            deps.context_trigger_tokens,
        )

    @staticmethod
    def _archive(deps: Any, evicted: tuple[Any, ...]) -> list[str]:
        try:
            if deps.context_sources_available:
                from inqtrix.agents.kernel.transcript import public_message_dump

                rendered = json.dumps(
                    [public_message_dump(message) for message in evicted], ensure_ascii=False
                )
                reference = deps.append_private_context_archive(
                    "Compacted history", rendered, list(evicted), "compaction_json"
                )
            else:
                rendered = "\n\n".join(
                    f"[{getattr(message, 'type', '?')}] {message_text(message)}" for message in evicted
                )
                reference = deps.append_context_archive("Compacted history", rendered)
        except Exception as exc:
            raise AgentContextArchiveError(
                "Context archive is unavailable; the original history was preserved."
            ) from exc
        if not reference:
            raise AgentContextArchiveError("Context archive returned no readable reference.")
        return [reference]

    @staticmethod
    def _check_request(deps: Any, prompt: str) -> None:
        budget = int(deps.context_input_budget or 0)
        if budget and count_tokens_approximately([HumanMessage(content=prompt)]) > budget:
            raise AgentModelCapacityError(
                deps.model or "kernel",
                "context_compaction",
                "The history to condense does not fit the model's input budget; nothing was compacted.",
                reason=ModelCapacityReason.CONTEXT_WINDOW,
            )

    def _fold(
        self, request: Any, deps: Any, plan: CompactionPlan, summary: str, refs: list[str]
    ) -> CompactionRecord:
        record = request.state.get(COMPACTION_STATE)
        messages = list(request.messages)
        archive_refs = [*(record["archive_refs"] if record else []), *refs]
        message = render_summary_message(
            assignment=run_assignment(messages),
            question=str(getattr(deps, "question", "") or "").strip(),
            answers=[
                str(answer).strip()
                for answer in getattr(deps, "clarified_answers", None) or []
                if str(answer).strip()
            ],
            summary=summary,
            archive_refs=archive_refs,
            archive_prefix=deps.context_archive_prefix,
            todos=request.state.get("todos"),
            skills=loaded_skills(messages[: plan.cutoff]),
        )
        return {
            "cutoff": plan.cutoff,
            "summary": summary,
            "message": message,
            "archive_refs": archive_refs,
            "generation": (record["generation"] if record else 0) + 1,
            "size_after": 0,
        }

    def _view_after(self, request: Any, deps: Any, new: CompactionRecord) -> tuple[list[Any], int]:
        """The view a call would see after *new*, and its size."""
        count = view_counter(request.system_message, request.tools)
        view = visible_view(
            list(request.messages),
            new,
            trigger=int(deps.context_trigger_tokens or 0),
            count=count,
            scope=self._scope(request, deps),
        )
        return view, count(view)

    @staticmethod
    def _hold(deps: Any, plan: CompactionPlan) -> None:
        deps.compaction_hold_tokens = plan.tokens + regrowth(int(deps.context_trigger_tokens or 0))

    def _can_shrink(self, request: Any, deps: Any, plan: CompactionPlan) -> bool:
        """Whether a summary could shrink the view: the summary message without one must.

        Checked before the archive and the paid call, so a compaction that
        cannot land costs neither; the segment then holds as after one that
        did not shrink.
        """
        warn_if_the_assignment_crowds_the_trigger(
            run_assignment(list(request.messages)), int(deps.context_trigger_tokens or 0)
        )
        floor = self._fold(request, deps, plan, "", [unwritten_section(deps.context_archive_prefix)])
        _, size = self._view_after(request, deps, floor)
        if size < plan.tokens:
            return True
        self._hold(deps, plan)
        log.warning(
            "Kernel compaction would not shrink the view even without a summary (%d tokens "
            "before, %d after); no summary is requested, the uncompacted view goes out and this "
            "segment waits for %d tokens.",
            plan.tokens,
            size,
            deps.compaction_hold_tokens,
        )
        return False

    def _settle(self, request: Any, deps: Any, plan: CompactionPlan, new: CompactionRecord) -> list[Any] | None:
        """The new view, or ``None`` when it would not be smaller than the old one.

        A compaction that shrinks the view ends the hold an earlier one left.
        """
        view, size = self._view_after(request, deps, new)
        if size >= plan.tokens:
            self._hold(deps, plan)
            log.warning(
                "Kernel compaction did not shrink the view (%d tokens before, %d after); the "
                "uncompacted view goes out and this segment waits for %d tokens.",
                plan.tokens,
                size,
                deps.compaction_hold_tokens,
            )
            return None
        deps.compaction_hold_tokens = 0
        new["size_after"] = size
        return view

    @staticmethod
    def _emit(deps: Any, before: CompactionRecord | None, new: CompactionRecord, refs: list[str]) -> None:
        payload = {
            "archive_artifact_id": refs[0],
            "archive_section_ids": list(refs),
            "generation": new["generation"],
            "messages_summarized": new["cutoff"] - (before["cutoff"] if before else 0),
            "summary_chars": len(new["summary"]),
            "trigger_tokens": deps.context_trigger_tokens,
        }
        log.info(
            "Kernel context compacted: %d messages summarized (generation %d, archive %s, "
            "trigger %d tokens).",
            payload["messages_summarized"],
            new["generation"],
            refs[0],
            deps.context_trigger_tokens,
        )
        deps.emit(CONTEXT_COMPACTED_EVENT, payload)
        emit_narration(
            deps.emit,
            narration_id=narration_id(new["generation"], refs),
            kind="status",
            text=t(
                {"language": resolve_answer_language(deps.question).code},
                "kernel_context_compacted",
            ),
            phase="execution",
            guard=deps.guard_reader_text,
        )

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        deps = self._deps()
        if deps is None:
            return handler(request)
        plan = self._plan(request, deps)
        if plan.stalled:
            self._warn_stalled(deps, plan)
        if not plan.compact or not self._can_shrink(request, deps, plan):
            return handler(request.override(messages=plan.view))
        before = request.state.get(COMPACTION_STATE)
        prompt = summary_request(before["summary"] if before else "", plan.summarized)
        self._check_request(deps, prompt)
        refs = self._archive(deps, plan.evicted)
        with bind_purpose(COMPACTION_PURPOSE):
            reply = request.model.invoke(prompt)
        new = self._fold(request, deps, plan, _summary_text(reply, deps.model or "kernel"), refs)
        view = self._settle(request, deps, plan, new)
        if view is None:
            return handler(request.override(messages=plan.view))
        response = handler(request.override(messages=view))
        self._emit(deps, before, new, refs)
        return ExtendedModelResponse(
            model_response=response, command=Command(update={COMPACTION_STATE: new})
        )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        deps = self._deps()
        if deps is None:
            return await handler(request)
        plan = self._plan(request, deps)
        if plan.stalled:
            self._warn_stalled(deps, plan)
        if not plan.compact or not self._can_shrink(request, deps, plan):
            return await handler(request.override(messages=plan.view))
        before = request.state.get(COMPACTION_STATE)
        prompt = summary_request(before["summary"] if before else "", plan.summarized)
        self._check_request(deps, prompt)
        # Strictly one after the other: a failed archive must not leave a
        # paid summary call running beside it.
        refs = await asyncio.to_thread(self._archive, deps, plan.evicted)
        with bind_purpose(COMPACTION_PURPOSE):
            reply = await request.model.ainvoke(prompt)
        new = self._fold(request, deps, plan, _summary_text(reply, deps.model or "kernel"), refs)
        view = self._settle(request, deps, plan, new)
        if view is None:
            return await handler(request.override(messages=plan.view))
        response = await handler(request.override(messages=view))
        self._emit(deps, before, new, refs)
        return ExtendedModelResponse(
            model_response=response, command=Command(update={COMPACTION_STATE: new})
        )
