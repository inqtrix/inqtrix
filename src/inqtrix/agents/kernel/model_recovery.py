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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.types import Command
from pydantic import BaseModel

from inqtrix.agents.kernel.chat_bridge import REJECTED_TOOL_ARGUMENTS_FLAG
from inqtrix.agents.kernel.deps import kernel_deps
from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import (
    AgentContextArchiveError,
    AgentModelCapacityError,
    AgentStructuredOutputError,
    AgentToolArgumentsError,
    ModelCapacityReason,
)
from inqtrix.agents.narration import emit_narration
from inqtrix.i18n import t
from inqtrix.observability.context import bind_purpose
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.providers.base import (
    ProviderAttemptBudget,
    current_provider_attempt_budget,
    provider_attempt_scope,
)
from inqtrix.untrusted_content import untrusted_fence

from inqtrix.agents.context_sources import SOURCE_RECEIPT

MODEL_RECOVERY_EVENT = "inqtrix.agent.model.recovery"

CONTEXT_PROJECTIONS = "kernel_context_projections"
CONTEXT_PROJECTED_FLAG = "inqtrix_context_projection"


class ModelRecoveryState(AgentState):
    kernel_context_projections: NotRequired[Annotated[dict[str, str], PrivateStateAttr]]
    context_source_authorization: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


def _deps():
    try:
        return kernel_deps()
    except RuntimeError:
        return None


def _tool_content_key(message: ToolMessage) -> str:
    value = json.dumps(
        [message.id, message.tool_call_id, message.content],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def project_recovered_messages(messages, projections):
    """Project only the active window; ordinary turns never hash tool bodies."""
    if not projections:
        return list(messages)
    projected = []
    for message in messages:
        if isinstance(message, ToolMessage) and not message.additional_kwargs.get(
            CONTEXT_PROJECTED_FLAG
        ):
            key = _tool_content_key(message)
            if key in projections:
                message = message.model_copy(
                    update={
                        "content": projections[key],
                        "additional_kwargs": {
                            **message.additional_kwargs,
                            CONTEXT_PROJECTED_FLAG: True,
                        },
                    }
                )
        projected.append(message)
    return projected


RecoverableModelError = (
    AgentModelCapacityError,
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
        usage = {
            "input_tokens": turn.prompt_tokens,
            "output_tokens": turn.completion_tokens,
            "total_tokens": turn.prompt_tokens + turn.completion_tokens,
        }
        return turn.text, calls, problem, usage
    message = error.ai_message
    calls = [
        (str(call.get("id") or ""), str(call.get("name") or ""), json.dumps(call.get("args") or {}, ensure_ascii=False))
        for call in message.tool_calls
    ]
    text = message.content if isinstance(message.content, str) else ""
    return text, calls, f"{error.tool_name}: {error.source}", message.usage_metadata


def _rejected_messages(error: AgentToolArgumentsError | StructuredOutputValidationError):
    text, calls, problem, usage = _rejected_batch(error)
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
        self.projections = dict(request.state.get(CONTEXT_PROJECTIONS) or {})
        self.messages = project_recovered_messages(request.messages, self.projections)
        self.rejected: list[Any] = []
        self.projections_changed = False
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
        if isinstance(error, (AgentToolArgumentsError, StructuredOutputValidationError)):
            correction = _rejected_messages(error)
            self.rejected.extend(correction)
            self.messages = [*self.messages, *correction]
            self.notify("tool_arguments", 0)
            return True
        return self.reduce_context()

    def prepare(self):
        """Plan the assembled input; preserve complete messages when it fits."""
        limit = self.deps.context_input_budget if self.deps is not None else 0
        if not limit:
            return
        from langchain_core.utils.function_calling import convert_to_openai_tool

        schemas = [convert_to_openai_tool(tool) for tool in self.request.tools or []]
        overhead = count_tokens_approximately(
            [SystemMessage(content=json.dumps(schemas, ensure_ascii=False))]
            + ([self.request.system_message] if self.request.system_message else [])
        )
        while overhead + count_tokens_approximately(self.messages) > limit:
            self.check()
            if not self.reduce_context(preflight=True):
                raise AgentModelCapacityError(
                    self.deps.model or "kernel", "context_preflight",
                    "The complete input does not fit the model budget; original content was retained.",
                    reason=ModelCapacityReason.CONTEXT_WINDOW,
                )

    def reduce_context(self, *, preflight=False):
        # Recovery must never manufacture a pointer the current tool profile
        # cannot read. Authorization failures retain their original semantics.
        if self.deps is None or self.budget.remaining < 2:
            return False
        names = {
            tool.get("name", tool.get("function", {}).get("name"))
            if isinstance(tool, dict)
            else tool.name
            for tool in self.request.tools or []
        }
        if "read_canvas" not in names:
            return False
        try:
            self.deps.require_tool_allowed("read_canvas")
        except PermissionError:
            return False
        candidates = [
            message
            for message in self.messages
            if isinstance(message, ToolMessage)
            and not message.additional_kwargs.get(REJECTED_TOOL_ARGUMENTS_FLAG)
            and not message.additional_kwargs.get(CONTEXT_PROJECTED_FLAG)
        ]
        candidates.sort(key=lambda m: count_tokens_approximately([m]), reverse=True)
        if not candidates:
            return False
        message = candidates[0]
        original = json.dumps(message.model_dump(mode="json"), ensure_ascii=False)
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
        pointer = "; ".join(
            f"read_canvas(artifact_id='{item}')" for item in archive_ids
        )
        projection_input = [
            SystemMessage(content=(
                "Create a compact task-specific projection of the complete tool result. "
                "Preserve material findings, exceptions, status, dates, quantities and "
                "citation identifiers needed for the task. Do not select only the beginning. "
                "Return text only; do not call tools or invent missing evidence."
            )),
            HumanMessage(content=(
                resolve_answer_language(self.deps.question).directive
                + "\nTask:\n" + self.deps.question
                + "\n\nComplete tool result (JSON data):\n"
                + untrusted_fence(original, "tool-result")
            )),
        ]
        if self.deps.context_input_budget and count_tokens_approximately(projection_input) > self.deps.context_input_budget:
            # The original is durable. A projection that itself cannot fit is
            # not an attempted model call and must not burn the retry budget.
            return False
        failed_attempt = self.budget.used
        self.notify("context_budget" if preflight else "context_window", len(archive_ids), failed_attempt=failed_attempt)
        self.check()
        used = self.budget.used
        try:
            with bind_purpose("agent_context_projection"):
                projection_message = self.request.model.invoke(projection_input)
        finally:
            if self.budget.used == used:
                self.budget.used += 1
        if (
            not isinstance(projection_message.content, str)
            or not projection_message.content.strip()
            or projection_message.tool_calls
            or projection_message.invalid_tool_calls
        ):
            raise AgentStructuredOutputError(
                self.deps.model or "kernel",
                "context_projection",
                "Context projection did not return usable text.",
            )
        projection = (
            "Task-specific projection of the complete tool result:\n"
            + untrusted_fence(
                projection_message.content.strip(), "context_projection"
            )
            + "\n\n"
            + f"Full original: {pointer}. Read relevant original sections for "
            "detail or before asserting that requested evidence is missing."
        )
        candidate = message.model_copy(
            update={
                "content": projection,
                "additional_kwargs": {
                    **message.additional_kwargs,
                    CONTEXT_PROJECTED_FLAG: True,
                },
            }
        )
        if count_tokens_approximately([candidate]) >= count_tokens_approximately(
            [message]
        ):
            return False
        self.projections[_tool_content_key(message)] = projection
        self.projections_changed = True
        self.messages = [
            candidate if item is message else item for item in self.messages
        ]
        return True

    def notify(self, reason, archives, *, failed_attempt=None):
        if self.deps is None:
            return
        self.deps.emit(
            MODEL_RECOVERY_EVENT,
            {
                "reason": reason,
                "failed_attempt": self.budget.used
                if failed_attempt is None
                else failed_attempt,
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
        if self.projections_changed:
            update[CONTEXT_PROJECTIONS] = self.projections
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
