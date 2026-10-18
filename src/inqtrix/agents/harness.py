"""The ONE deepagents boundary.

Every deepagents import lives in this module: when the library churns
(and it can do so frequently), this file absorbs it. The rest
of the agent runtime calls the seam functions below and can be tested by
monkeypatching them — the trajectory catalog never enters deepagents.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from inqtrix.exceptions import AgentContextArchiveError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

log = logging.getLogger("inqtrix")

KERNEL_EXCLUDED_BUILTIN_TOOLS = frozenset(
    {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "delete",
        "execute",
    }
)
"""deepagents built-ins the kernel never exposes.

The kernel's world is Inqtrix services (knowledge, web, canvas, child
runs) — a filesystem/sandbox surface would bypass every capability
check. ``write_todos`` is no deepagents built-in here: the harness adds
the planner to the conversational kernel only, where it feeds the
task-list display (see :func:`build_kernel_agent`). ``delete``/``execute``
only exist with a sandbox backend but
excluding them is free future-proofing; the contract test asserts the
REMAINING built-in set, so an upstream addition fails loudly."""

_kernel_profile_lock = threading.Lock()
_kernel_profile_registered = False


def _register_kernel_harness_profile() -> None:
    """Register the kernel's harness profile once per process.

    deepagents resolves profiles via the model's ``provider:identifier``
    key — the bridge model declares ``inqtrix:kernel`` (see
    :mod:`inqtrix.agents.kernel.chat_bridge`). Registration is global and
    additive-merge, so repeating the identical profile would be harmless;
    the guard exists to keep the registry deterministic under concurrent
    worker threads.
    """
    global _kernel_profile_registered
    with _kernel_profile_lock:
        if _kernel_profile_registered:
            return
        from deepagents import (
            GeneralPurposeSubagentProfile,
            HarnessProfile,
            register_harness_profile,
        )

        from inqtrix.agents.kernel.chat_bridge import (
            KERNEL_MODEL_IDENTIFIER,
            KERNEL_MODEL_PROVIDER,
        )

        from deepagents.middleware.summarization import (
            _DeepAgentsSummarizationMiddleware,
        )

        register_harness_profile(
            f"{KERNEL_MODEL_PROVIDER}:{KERNEL_MODEL_IDENTIFIER}",
            HarnessProfile(
                excluded_tools=KERNEL_EXCLUDED_BUILTIN_TOOLS,
                # Strip the base-stack summarization (class-form exclusion
                # matches the exact type, so the kernel's SUBCLASS in the
                # user slot survives): its trigger never fires before the
                # kernel's ceilings, it is invisible (no inqtrix event),
                # and its offload points at read_file — excluded above.
                excluded_middleware=frozenset(
                    {_DeepAgentsSummarizationMiddleware}
                ),
                general_purpose_subagent=GeneralPurposeSubagentProfile(
                    enabled=False
                ),
            ),
        )
        _kernel_profile_registered = True


_kernel_summarization_cls: type | None = None


def _kernel_summarization_middleware_cls() -> type:
    """The kernel's summarization subclass (lazy: deepagents boundary).

    Defined inside the one deepagents-importing module so
    ``inqtrix.agents.kernel.middleware`` stays importable without the
    ``agent`` extra. Cached so the class identity is stable per process
    (the graph cache may build several policy variants).
    """
    global _kernel_summarization_cls
    if _kernel_summarization_cls is not None:
        return _kernel_summarization_cls

    from deepagents.middleware.summarization import (
        _DeepAgentsSummarizationMiddleware,
    )
    from langchain_core.messages import HumanMessage

    from inqtrix.agents.kernel.middleware import (
        SUMMARY_CHARS_KEY,
        SUMMARY_FAILED_KEY,
        current_compaction_assignment,
        current_compaction_todos,
        emit_compaction_event,
        message_text,
        render_todo_state,
        run_assignment,
    )

    class KernelSummarizationMiddleware(_DeepAgentsSummarizationMiddleware):
        """The kernel's ONE summarization: ledger-grounded and loud.

        Replaces (never duplicates) the deepagents base-stack instance —
        the harness profile excludes the base CLASS and exact-type
        matching preserves this subclass in the user slot. Three
        deviations, each closing a named gap:

        1. **Ledger-grounded offload** — evicted history is appended to
           the run's ``context_archive`` artifact instead of a backend
           file the kernel cannot read back (``read_file`` is excluded);
           the summary embeds a ``read_canvas`` pointer.
        2. **Per-run trigger through the deps ContextVar** — the compiled
           graph is shared across runs and models, so the threshold comes
           from ``kernel_deps().context_trigger_tokens`` at call time
           (0 disables), never from constructor state.
        3. **Visible compaction** — every compaction emits
           ``inqtrix.agent.context.compacted`` plus a deterministic
           narration, and the summary message recites the current
           ``write_todos`` state (goal recitation exactly where the
           prompt-cache prefix breaks anyway).

        The base implementation is non-mutating (``state["messages"]``
        and their ``usage_metadata`` stay intact), so the token-budget
        basis (``_checkpointed_usage``) and replay survive compaction.

        The provider bridge preserves Inqtrix capacity errors. Translating
        them to ``ContextOverflowError`` would activate the base recovery
        branch, which offloads large tool results to ``StateBackend`` files
        and inserts ``read_file`` pointers. Those pointers are unavailable
        to the kernel; proactive compaction uses the run archive instead.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            # The provider already owns retries and their operation deadline.
            # LangChain's with_retry wrapper would multiply that allowance
            # and retry cancellation or permanent errors outside that owner.
            self._lc_helper._summary_model = self.model

        @staticmethod
        def _deps() -> Any | None:
            from inqtrix.agents.kernel.deps import kernel_deps

            try:
                return kernel_deps()
            except RuntimeError:
                return None

        def _should_summarize(
            self, messages: list[Any], total_tokens: int
        ) -> bool:
            deps = self._deps()
            if deps is None or deps.context_trigger_tokens <= 0:
                return False
            return total_tokens >= deps.context_trigger_tokens

        def _get_effective_messages(self, request: Any) -> list[Any]:
            from inqtrix.agents.kernel.model_recovery import (
                CONTEXT_PROJECTIONS,
                project_recovered_messages,
            )

            return project_recovered_messages(
                super()._get_effective_messages(request),
                request.state.get(CONTEXT_PROJECTIONS) or {},
            )

        def _offload_to_backend(
            self, backend: Any, messages: list[Any], session_id: str
        ) -> str | None:
            """Archive evicted history in the run artifact, not a file."""
            # The run-bound deps own the archive identity. The SDK's session
            # id names its filesystem history, which this kernel excludes.
            del backend, session_id
            deps = self._deps()
            if deps is None:
                return None
            try:
                # A chained compaction's eviction window contains the
                # PRIOR summary message — the base filters it via
                # _is_summary_message before offloading, so this
                # replacement must too, or every later compaction
                # re-archives the previous summary as ordinary history.
                evicted = [
                    message
                    for message in messages
                    if (
                        getattr(message, "additional_kwargs", None) or {}
                    ).get("lc_source")
                    != "summarization"
                ]
                if not evicted:
                    return None
                rendered = "\n\n".join(
                    f"[{getattr(message, 'type', '?')}] "
                    f"{message_text(message)}"
                    for message in evicted
                )
                # One logical source reference spans its immutable storage
                # parts. read_canvas supplies version-bound continuations.
                if deps.context_sources_available:
                    rendered = json.dumps([message.model_dump(mode="json") for message in evicted], ensure_ascii=False)
                    archive_id = deps.append_private_context_archive("Compacted history", rendered, evicted, "compaction_json")
                else:
                    archive_id = deps.append_context_archive("Compacted history", rendered)
                if not archive_id:
                    raise AgentContextArchiveError("Context archive returned no readable reference.")
                return archive_id
            except Exception as exc:
                raise AgentContextArchiveError(
                    "Context archive is unavailable; the original history was preserved."
                ) from exc

        async def _aoffload_to_backend(
            self, backend: Any, messages: list[Any], session_id: str
        ) -> str | None:
            """Use the same archive contract without blocking the event loop."""
            return await asyncio.to_thread(
                self._offload_to_backend, backend, messages, session_id
            )

        def _build_new_messages_with_path(
            self, summary: str, file_path: str | None
        ) -> list[Any]:
            from inqtrix.untrusted_content import untrusted_fence

            # A compaction must not change what is instruction and what is
            # data. The summary condenses older messages, tool outputs
            # included: fenced text goes in, so it comes back fenced. The
            # SDK condenses everything before its cutoff, the run's first
            # messages included, and keeps only this message — so the
            # assignment and the user's answers travel here, verbatim and
            # OUTSIDE the fence, read from the run, never from the summary.
            run_deps = self._deps()
            assignment = current_compaction_assignment.get()
            parts: list[str] = []
            if assignment:
                parts.append(
                    "The assignment of this run, verbatim as it was given "
                    "(it still directs your work):\n" + assignment
                )
                self._warn_if_the_assignment_crowds_the_trigger(
                    assignment, run_deps
                )
            else:
                task = str(getattr(run_deps, "question", "") or "").strip()
                log.warning(
                    "Kernel compaction without the run's assignment in reach; "
                    "only the question is restated outside the summary."
                )
                if task:
                    parts.append(f"Your task (restated from the run):\n{task}")
            answers = [
                str(answer).strip()
                for answer in getattr(run_deps, "clarified_answers", None) or []
                if str(answer).strip()
            ]
            if answers:
                parts.append(
                    "The user's answers to your questions in this run:\n"
                    + "\n\n".join(answers)
                )
            parts.append(
                "Summary of the conversation so far (a condensation of "
                "earlier messages and tool outputs):\n"
                + untrusted_fence(summary, "conversation-summary")
            )
            if file_path:
                sections = [
                    part for part in file_path.split(",") if part
                ]
                listed = "; ".join(
                    f"read_canvas(artifact_id='{section}')"
                    for section in sections
                )
                pointer = (
                    "The older conversation is stored as a section in the "
                    f"run archive: {listed}."
                    if len(sections) <= 1
                    else (
                        f"The older conversation is stored in {len(sections)} "
                        f"sections in the run archive: {listed}."
                    )
                )
                deps = self._deps()
                if deps is not None:
                    pointer += (
                        " All archive sections: read_canvas("
                        f"artifact_id='{deps.context_archive_prefix}')."
                    )
                parts.append(pointer)
            todos = render_todo_state(current_compaction_todos.get())
            if todos:
                parts.append(f"Current task state:\n{todos}")
            return [
                HumanMessage(
                    content="\n\n".join(parts),
                    # The base's _is_summary_message keys on this kwarg;
                    # without it a SECOND compaction re-archives the
                    # prior summary as ordinary history (duplicates).
                    additional_kwargs={
                        "lc_source": "summarization",
                        SUMMARY_CHARS_KEY: len(summary),
                        SUMMARY_FAILED_KEY: "Error generating summary:" in summary,
                    },
                )
            ]

        @staticmethod
        def _warn_if_the_assignment_crowds_the_trigger(
            assignment: str, run_deps: Any
        ) -> None:
            """The assignment is passed uncondensed; say when it is big."""
            from langchain_core.messages.utils import count_tokens_approximately

            trigger = int(getattr(run_deps, "context_trigger_tokens", 0) or 0)
            if trigger <= 0:
                return
            tokens = count_tokens_approximately([HumanMessage(content=assignment)])
            if tokens * 2 > trigger:
                log.warning(
                    "The run's assignment (~%d tokens) takes more than half of the "
                    "compaction trigger (%d tokens); it stays verbatim, so the run "
                    "may compact on almost every turn.",
                    tokens,
                    trigger,
                )

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            before = request.state.get("_summarization_event")
            token = current_compaction_todos.set(
                request.state.get("todos")
            )
            assignment_token = current_compaction_assignment.set(
                run_assignment(request.state.get("messages"))
            )
            try:
                response = super().wrap_model_call(request, handler)
            finally:
                current_compaction_assignment.reset(assignment_token)
                current_compaction_todos.reset(token)
            self._emit_compaction(before, response)
            return response

        @staticmethod
        def _emit_compaction(before: Any, response: Any) -> None:
            command = getattr(response, "command", None)
            update = getattr(command, "update", None)
            after = (
                update.get("_summarization_event")
                if isinstance(update, dict)
                else None
            )
            if isinstance(after, dict) and after is not before:
                emit_compaction_event(before, after)

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            before = request.state.get("_summarization_event")
            token = current_compaction_todos.set(
                request.state.get("todos")
            )
            assignment_token = current_compaction_assignment.set(
                run_assignment(request.state.get("messages"))
            )
            try:
                response = await super().awrap_model_call(request, handler)
            finally:
                current_compaction_assignment.reset(assignment_token)
                current_compaction_todos.reset(token)
            self._emit_compaction(before, response)
            return response

    _kernel_summarization_cls = KernelSummarizationMiddleware
    return KernelSummarizationMiddleware


def _kernel_summarization_middleware(
    chat_model: Any, *, keep_messages: int
) -> Any:
    """One configured kernel summarization instance (user middleware slot)."""
    from deepagents.backends import StateBackend

    from inqtrix.agents.kernel.middleware import KERNEL_SUMMARY_PROMPT

    cls = _kernel_summarization_middleware_cls()
    return cls(
        chat_model,
        # The backend is unused (the override archives into the run
        # artifact), but the base constructor requires one.
        backend=StateBackend(),
        trigger=None,
        keep=("messages", keep_messages),
        summary_prompt=KERNEL_SUMMARY_PROMPT,
        # The LangChain default (4000, strategy "last") would let the
        # summary model see only the newest sliver of the evicted
        # history — the citation-token preservation the summary prompt
        # demands needs the WHOLE eviction (the archive caps per
        # SECTION, visibly; cost is bounded by the compaction trigger
        # itself).
        trim_tokens_to_summarize=None,
    )


def build_kernel_agent(
    chat_model: Any,
    *,
    tools: "Sequence[Any]",
    system_prompt: str,
    interrupt_on: "Mapping[str, Any] | None" = None,
    checkpointer: Any = None,
    max_tool_calls: int | None = None,
    context_keep_messages: int | None = None,
    response_format: Any = None,
) -> Any:
    """Assemble the kernel loop — the ONE ``create_deep_agent`` call.

    Args:
        chat_model: Tool-calling bridge from
            :func:`inqtrix.agents.kernel.chat_bridge.build_tool_chat_model`
            (its declared ``inqtrix:kernel`` identity selects the kernel
            harness profile registered here).
        tools: Sync LangChain tools; the Inqtrix tool surface. Anonymous
            sub-agent fan-out stays off — fan-out happens via budgeted
            child runs only.
        system_prompt: Full German kernel system prompt.
        interrupt_on: Tool-name -> HITL config mapping for policy gates;
            ``None``/empty omits the HITL middleware entirely (auto
            policy). Payload/resume shapes are frozen by the contract
            tests (the deepagents upgrade gate).
        checkpointer: LangGraph checkpointer; REQUIRED for park/resume
            (``thread_id=run_id``). ``None`` is only for throwaway
            in-test agents.
        max_tool_calls: Run-wide kernel tool-call ceiling. Counts are
            recovered from checkpointed AI messages, so park/resume is
            cumulative and idempotent. ``None`` is only for low-level
            harness tests that do not exercise the production kernel.
        context_keep_messages: Newest messages kept verbatim per
            compaction; enables the kernel summarization middleware
            (the run-time trigger comes from the deps ContextVar).
            ``None`` builds an agent without compaction — only for
            low-level harness tests, mirroring ``max_tool_calls``.
        response_format: Typed final result of a role run (a framework
            ``ToolStrategy``). The loop then ends with the validated result
            in ``structured_response`` instead of answer text. Its call
            executes nothing, so it spends no tool budget, and the framework
            binds every model turn to call a tool. Such a graph gets no
            planner (``write_todos``): the planner's prompt asks for a
            closing text answer, which a role never gives. ``None`` for the
            conversational kernel, which keeps the planner.

    Returns:
        The compiled deepagents graph (sync ``stream``/``invoke`` only —
        never ``ainvoke``; async paths are the documented upstream bug
        surface on LiteLLM/Azure).

    Raises:
        RuntimeError: deepagents is not installed (missing ``agent``
            extra).
    """
    try:
        from deepagents import create_deep_agent
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "The agent kernel requires the 'agent' extra (uv sync --extra agent)."
        ) from exc
    _register_kernel_harness_profile()
    from langchain.agents.middleware import TodoListMiddleware
    from deepagents.middleware.filesystem import FilesystemMiddleware

    from inqtrix.agents.kernel.middleware import KernelSkillInputMiddleware

    from inqtrix.agents.kernel.middleware import (
        KernelChildBatchGuardMiddleware,
        KernelModelTurnMiddleware,
        KernelSufficiencyMiddleware,
        KernelToolSpanMiddleware,
    )

    middleware: list[Any] = [
        # Replace the SDK instance in place. Its automatic tool/human evictions
        # would insert read_file pointers even though that tool is excluded.
        # Kernel archives and context recovery own this responsibility. With
        # eviction off its tool wrap is a pass-through; the tool span below
        # relies on that, because this layer stays outside it.
        FilesystemMiddleware(
            tool_token_limit_before_evict=None,
            human_message_token_limit_before_evict=None,
        ),
        # First kernel entry, so its tool wrap encloses every layer that can
        # change a tool result (a trace records the call it is handed). It
        # has no model hook, so the model chain below is unchanged.
        KernelToolSpanMiddleware(),
        # Outermost wrap so the model-turn activity brackets EVERYTHING the
        # model boundary does (incl. a compaction pass). wrap_model_call
        # only — a node-creating hook would re-price the pinned supersteps.
        KernelModelTurnMiddleware(),
        # The planner serves the conversation's task list. A role graph
        # (typed result) has none: its prompt tells the model to close with
        # a text answer, which a role must never give, and its after_model
        # node is one super-step less per turn (the role ceiling priced with
        # the conversational constant stays on the safe side).
        *([TodoListMiddleware()] if response_format is None else []),
        KernelSkillInputMiddleware(),
        # The advisory sufficiency nudge is always compiled
        # in — the runtime flag short-circuits inside the hook, so the
        # per-turn super-step price stays constant across deployments
        # (the recursion ceilings are priced against it).
        KernelSufficiencyMiddleware(),
        # Pre-dispatch: >1 child-tool calls in ONE model turn would each
        # submit-then-interrupt and trip the multi-interrupt guard AFTER
        # the submissions (orphaned children) — the guard rewrites the
        # batch into corrective ToolMessages before anything dispatches.
        KernelChildBatchGuardMiddleware(),
    ]
    if max_tool_calls is not None:
        from inqtrix.agents.kernel.middleware import (
            KernelToolBudgetMiddleware,
        )

        middleware.append(
            KernelToolBudgetMiddleware(
                max_tool_calls, uncounted_tools=result_call_names(response_format)
            )
        )
    if context_keep_messages is not None:
        middleware.append(
            _kernel_summarization_middleware(
                chat_model, keep_messages=context_keep_messages
            )
        )
    from inqtrix.agents.kernel.model_recovery import KernelModelRecoveryMiddleware

    middleware.append(KernelModelRecoveryMiddleware())
    return create_deep_agent(
        model=chat_model,
        tools=list(tools),
        system_prompt=system_prompt,
        interrupt_on=dict(interrupt_on) if interrupt_on else None,
        checkpointer=checkpointer,
        middleware=middleware,
        response_format=response_format,
    )


def result_call_names(response_format: Any) -> frozenset[str]:
    """Call names under which the framework delivers a typed final result."""
    if response_format is None:
        return frozenset()
    return frozenset(spec.name for spec in response_format.schema_specs)


def deepagents_available() -> bool:
    """Whether the optional ``agent`` extra's harness import works."""
    try:
        import deepagents  # noqa: F401
    except ImportError:
        return False
    return True
