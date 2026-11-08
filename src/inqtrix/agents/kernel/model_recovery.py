"""Bounded model corrections within the existing kernel model boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from typing import Annotated, Any, NotRequired

from langchain.agents.structured_output import StructuredOutputValidationError
from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command
from pydantic import BaseModel

from inqtrix.agents.kernel.chat_bridge import (
    PROVIDER_STATE_KWARG,
    REJECTED_TOOL_ARGUMENTS_FLAG,
    THREAD_CALL_IDS,
    turn_usage_metadata,
    unique_call_ids,
    used_call_ids,
)
from inqtrix.agents.kernel.compaction import (
    CLEAR_KEEP,
    CLEARED_RESULT_FLAG,
    NEVER_CLEARED_TOOLS,
    RereadScope,
    archive_reread,
    placeholder_for,
    rereadable_original,
    tool_calls_by_id,
    tool_names,
    unwritten_section,
    view_counter,
)
from inqtrix.agents.kernel.deps import kernel_deps
from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import (
    AgentContextArchiveError,
    AgentModelCapacityError,
    AgentRequiredToolCallMissing,
    AgentToolArgumentsError,
    ModelCapacityReason,
)
from inqtrix.agents.narration import emit_narration
from inqtrix.i18n import t
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.providers.base import (
    ProviderAttemptBudget,
    current_provider_attempt_budget,
    provider_attempt_scope,
)

from inqtrix.agents.context_sources import SOURCE_RECEIPT
from inqtrix.harness.contracts.events import MODEL_RECOVERY_EVENT


class ModelRecoveryState(AgentState):
    context_source_authorization: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


def _deps():
    try:
        return kernel_deps()
    except RuntimeError:
        return None


RecoverableModelError = (
    AgentModelCapacityError,
    AgentRequiredToolCallMissing,
    AgentToolArgumentsError,
    StructuredOutputValidationError,
)
"""Identified model failures the bounded recovery repairs; everything else propagates."""


def _rejected_batch(error: AgentToolArgumentsError | StructuredOutputValidationError):
    """One rejected generation as ``(text, calls, problem, usage)``.

    Unparsable arguments are caught by the provider, a typed result that
    violates its schema by the framework. Both are the same failure — the
    model requested a call whose arguments cannot be used — and get the
    same feedback within the same attempt budget.
    """
    if isinstance(error, AgentToolArgumentsError):
        turn = error.turn
        calls = [
            (call["id"], call["function"]["name"], call["function"]["arguments"])
            for call in error.raw_tool_calls
        ]
        problem = " ".join(dict.fromkeys(error.validation_errors))
        return turn.text, calls, problem, turn_usage_metadata(turn)
    message = error.ai_message
    calls = [
        (str(call.get("id") or ""), str(call.get("name") or ""), json.dumps(call.get("args") or {}, ensure_ascii=False))
        for call in message.tool_calls
    ]
    text = message.content if isinstance(message.content, str) else ""
    return text, calls, f"{error.tool_name}: {error.source}", message.usage_metadata


def _missing_call_messages(error: AgentRequiredToolCallMissing):
    """The text answer and the obligation it missed, for a bounded correction."""
    turn = error.turn
    names = ", ".join(error.tool_names) or "the available tools"
    return [
        AIMessage(
            content=turn.text,
            usage_metadata=turn_usage_metadata(turn),
            additional_kwargs={PROVIDER_STATE_KWARG: turn.provider_state} if turn.provider_state else {},
        ),
        HumanMessage(
            content=(
                f"Error: this step must end with a call to one of these tools: {names}. "
                "Call one of them now with your result instead of answering in text."
            )
        ),
    ]


def _rejected_state(error: AgentToolArgumentsError | StructuredOutputValidationError):
    """The provider's state of the rejected generation, which goes back with it."""
    if isinstance(error, AgentToolArgumentsError):
        return error.turn.provider_state
    return (error.ai_message.additional_kwargs or {}).get(PROVIDER_STATE_KWARG)


def _rejected_messages(
    error: AgentToolArgumentsError | StructuredOutputValidationError, *, used: set[str] | frozenset[str]
):
    """The rejected generation and one error result per call, under ids new to the thread."""
    text, calls, problem, usage = _rejected_batch(error)
    call_ids = unique_call_ids([call_id for call_id, _, _ in calls], used=set(used))
    calls = [(call_id, name, arguments) for call_id, (_, name, arguments) in zip(call_ids, calls)]
    state = _rejected_state(error)
    feedback = (
        "Error: Tool arguments were rejected before execution. "
        + problem
        + " No tool in this batch was executed. Return a corrected call using "
        "the same available tools and their argument schema."
    )
    rejected = AIMessage(
        content=text,
        invalid_tool_calls=[
            {"id": call_id, "name": name, "args": arguments, "error": problem}
            for call_id, name, arguments in calls
        ],
        usage_metadata=usage,
        additional_kwargs={PROVIDER_STATE_KWARG: state} if state else {},
    )
    return [
        rejected,
        *[
            ToolMessage(
                content=feedback,
                name=name,
                tool_call_id=call_id,
                status="error",
                additional_kwargs={REJECTED_TOOL_ARGUMENTS_FLAG: True},
            )
            for call_id, name, _ in calls
        ],
    ]


class _RecoveryCall:
    """Mutable state belongs to one invocation, never to the shared graph."""

    def __init__(self, request):
        self.request = request
        self.deps = _deps()
        timeout = self.deps.timeout if self.deps is not None else REASONING_TIMEOUT
        self.budget = current_provider_attempt_budget() or ProviderAttemptBudget(
            deadline=time.monotonic() + timeout
        )
        self.messages = list(request.messages)
        self.rejected: list[Any] = []
        last = request.messages[-1] if request.messages else None
        self.identity = hashlib.sha256(
            str(
                getattr(last, "id", None) or getattr(last, "tool_call_id", "model")
            ).encode()
        ).hexdigest()[:16]

    def check(self):
        if self.deps is not None:
            self.deps.check_abort()
        self.budget.check()

    def recover(self, error):
        if (
            isinstance(error, AgentModelCapacityError)
            and error.reason is not ModelCapacityReason.CONTEXT_WINDOW
        ):
            return False
        self.check()
        if isinstance(error, AgentRequiredToolCallMissing):
            correction = _missing_call_messages(error)
            self.rejected.extend(correction)
            self.messages = [*self.messages, *correction]
            self.notify("required_tool", 0)
            return True
        if isinstance(error, (AgentToolArgumentsError, StructuredOutputValidationError)):
            correction = _rejected_messages(
                error,
                used=used_call_ids(self.messages)
                | frozenset(self.request.model_settings.get(THREAD_CALL_IDS) or ()),
            )
            self.rejected.extend(correction)
            self.messages = [*self.messages, *correction]
            self.notify("tool_arguments", 0)
            return True
        archives = self.reduce_context()
        if archives is None:
            return False
        self.notify("context_window", archives)
        return True

    def prepare(self):
        """Plan the assembled input; preserve complete messages when it fits."""
        limit = self.deps.context_input_budget if self.deps is not None else 0
        if not limit:
            return
        # The compaction trigger counts the same way, so the two never
        # disagree near the budget.
        count = view_counter(self.request.system_message, self.request.tools)
        cleared = archives = 0
        while count(self.messages) > limit:
            self.check()
            written = self.reduce_context()
            if written is None:
                raise AgentModelCapacityError(
                    self.deps.model or "kernel", "context_preflight",
                    "The complete input does not fit the model budget; original content was retained.",
                    reason=ModelCapacityReason.CONTEXT_WINDOW,
                )
            cleared += 1
            archives += written
        if cleared:
            self.notify("context_budget", archives)

    def reduce_context(self):
        """Clear one tool result of this call, in the order stage one protects them.

        First a result the model has answered outside the newest three, then
        one of the newest three it has answered, last one it has not seen yet,
        whose placeholder then names no evidence; the largest first in each.
        A result with no reader in this call, or one a placeholder would not
        shrink, is passed over. The placeholder names how to read the result
        again; a result that cannot be read again goes to the run archive
        first. Nothing goes into the graph state and no model is called: the
        next call meets the same results and clears them the same way, unless
        stage one has cleared them by then; the archive writes such a result
        as a second section when the run-wide data of its transcript
        descriptor changed in between. Without state a result hidden here
        counts as answered once the model has replied after it, so a later call
        gives it the answered form.

        Returns:
            The archive parts written (0 or 1), or ``None`` when no result
            can be cleared.
        """
        if self.deps is None:
            return None
        scope = RereadScope(self.deps.context_archive_prefix, tool_names(self.request.tools))
        calls = tool_calls_by_id(self.messages)
        for message, seen in self._candidates():
            reread = rereadable_original(message, calls, scope)
            if reread is not None and not self.readable(reread.tool):
                reread = None
            if reread is None:
                # Recovery must never manufacture a pointer the current tool
                # profile cannot read. Authorization failures retain their
                # original semantics.
                if "read_canvas" not in scope.tools or not self.readable("read_canvas"):
                    continue
                sized = archive_reread(unwritten_section(scope.archive_prefix))
            else:
                sized = reread
            cleared = placeholder_for(message, sized.hint, seen=seen)
            if count_tokens_approximately([cleared]) >= count_tokens_approximately([message]):
                continue
            archives = 0
            if reread is None:
                cleared = placeholder_for(message, archive_reread(self.archive(message)).hint, seen=seen)
                archives = 1
            self.messages = [cleared if item is message else item for item in self.messages]
            return archives
        return None

    def _candidates(self):
        """The results that may give way, each with whether the model has seen it, in order."""
        answered = max(
            (index for index, message in enumerate(self.messages) if isinstance(message, AIMessage)),
            default=-1,
        )
        results = [
            index
            for index, message in enumerate(self.messages)
            if isinstance(message, ToolMessage)
            and message.name not in NEVER_CLEARED_TOOLS
            and not message.additional_kwargs.get(CLEARED_RESULT_FLAG)
        ]
        newest = set(results[-CLEAR_KEEP:])

        def order(index):
            rank = 2 if index > answered else 1 if index in newest else 0
            return rank, -count_tokens_approximately([self.messages[index]])

        for index in sorted(results, key=order):
            message = self.messages[index]
            if not message.additional_kwargs.get(REJECTED_TOOL_ARGUMENTS_FLAG):
                yield message, index < answered

    def readable(self, tool_name):
        try:
            self.deps.require_tool_allowed(tool_name)
        except PermissionError:
            return False
        return True

    def archive(self, message):
        from inqtrix.agents.kernel.transcript import public_message_dump

        original = json.dumps(public_message_dump(message), ensure_ascii=False)
        try:
            if self.deps.context_sources_available:
                archive_ids = [self.deps.append_private_context_archive(
                    "Model context recovery", original, [message], "message_json",
                )]
            else:
                archive_ids = self.deps.append_context_archive_chunked("Model context recovery", original)
        except Exception as exc:
            raise AgentContextArchiveError(
                "Context could not be archived; the original messages were preserved."
            ) from exc
        if not archive_ids:
            raise AgentContextArchiveError(
                "Context archive returned no readable reference."
            )
        return archive_ids[0]

    def notify(self, reason, archives):
        if self.deps is None:
            return
        self.deps.emit(
            MODEL_RECOVERY_EVENT,
            {
                "reason": reason,
                "failed_attempt": self.budget.used,
                "max_attempts": self.budget.max_attempts,
                "archive_count": archives,
            },
        )
        emit_narration(
            self.deps.emit,
            narration_id=(
                f"recovery_{self.identity}_{reason}_{self.budget.used}"
            ),
            kind="status",
            text=t(
                {"language": resolve_answer_language(self.deps.question).code},
                f"kernel_recovery_{reason}",
            ),
            phase="execution",
            guard=self.deps.guard_reader_text,
        )

    def finish(self, response):
        if self.deps is not None:
            self.deps.check_source_authority()
        # The innermost model handler has the canonical ModelResponse shape.
        # Rejected generations and their feedback become ordinary checkpointed
        # messages; the final valid batch alone reaches normal dispatch.
        if self.rejected:
            response = replace(response, result=[*self.rejected, *response.result])
        structured = getattr(response, "structured_response", None)
        if isinstance(structured, BaseModel):
            # The framework validated the typed result of a role run; the
            # graph state keeps its plain data. A checkpoint that holds only
            # framework-native types survives serializer allowlists.
            response = replace(
                response, structured_response=structured.model_dump(mode="json")
            )
        update = {}
        receipt = self.deps.source_receipt() if self.deps is not None else {}
        if receipt and receipt != self.request.state.get(SOURCE_RECEIPT):
            update[SOURCE_RECEIPT] = receipt
        if update:
            return ExtendedModelResponse(model_response=response, command=Command(update=update))
        return response


class KernelModelRecoveryMiddleware(AgentMiddleware):
    """Repair only identified model failures within the provider's budget."""

    state_schema = ModelRecoveryState

    def wrap_model_call(
        self, request, handler
    ) -> ModelResponse | ExtendedModelResponse:
        call = _RecoveryCall(request)
        with provider_attempt_scope(call.budget):
            call.prepare()
            while True:
                call.check()
                used = call.budget.used
                try:
                    try:
                        response = handler(request.override(messages=call.messages))
                    finally:
                        # Custom providers may not use the bundled SDK retry
                        # helper. They still spend one attempt per chat call.
                        if call.budget.used == used:
                            call.budget.used += 1
                except RecoverableModelError as exc:
                    if not call.budget.remaining or not call.recover(exc):
                        raise
                else:
                    return call.finish(response)

    async def awrap_model_call(
        self, request, handler
    ) -> ModelResponse | ExtendedModelResponse:
        call = _RecoveryCall(request)
        with provider_attempt_scope(call.budget):
            await asyncio.to_thread(call.prepare)
            while True:
                call.check()
                used = call.budget.used
                try:
                    try:
                        response = await handler(
                            request.override(messages=call.messages)
                        )
                    finally:
                        if call.budget.used == used:
                            call.budget.used += 1
                except RecoverableModelError as exc:
                    if not call.budget.remaining:
                        raise
                    if not await asyncio.to_thread(call.recover, exc):
                        raise
                else:
                    return call.finish(response)
