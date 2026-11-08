"""The ONE build of the kernel graph: LangChain ``create_agent`` with the kernel's state.

The kernel loop is the framework's agent loop; this module assembles it from
the kernel's middleware stack and :class:`inqtrix.agents.kernel.state.KernelState`.
The rest of the agent runtime calls the seam functions below and can be tested
by monkeypatching them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


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
    """Assemble the kernel loop — the ONE ``create_agent`` call.

    Args:
        chat_model: Tool-calling bridge from
            :func:`inqtrix.agents.kernel.chat_bridge.build_tool_chat_model`.
        tools: Sync LangChain tools; the Inqtrix tool surface. Anonymous
            sub-agent fan-out stays off — fan-out happens via budgeted
            child runs only.
        system_prompt: Full German kernel system prompt.
        interrupt_on: Tool-name -> HITL config mapping for policy gates;
            ``None``/empty omits the HITL middleware entirely (auto
            policy). It is the last entry of the stack. Payload/resume
            shapes are frozen by the contract tests.
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
        The compiled graph (sync ``stream``/``invoke`` only — never
        ``ainvoke``; async paths are the documented upstream bug surface
        on LiteLLM/Azure).

    Raises:
        RuntimeError: LangChain is not installed (missing ``agent`` extra).
    """
    try:
        from langchain.agents import create_agent
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "The agent kernel requires the 'agent' extra (uv sync --extra agent)."
        ) from exc
    from langchain.agents.middleware import HumanInTheLoopMiddleware, TodoListMiddleware

    from inqtrix.agents.kernel.middleware import KernelSkillInputMiddleware
    from inqtrix.agents.kernel.state import KernelState

    from inqtrix.agents.kernel.middleware import (
        KernelChildBatchGuardMiddleware,
        KernelModelTurnMiddleware,
        KernelSufficiencyMiddleware,
        KernelToolSpanMiddleware,
    )

    middleware: list[Any] = [
        # First entry, so its tool wrap encloses every layer that can
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
        from inqtrix.agents.kernel.compaction import InqtrixCompactionMiddleware

        middleware.append(
            InqtrixCompactionMiddleware(keep_messages=context_keep_messages)
        )
    from inqtrix.agents.kernel.model_recovery import KernelModelRecoveryMiddleware

    middleware.append(KernelModelRecoveryMiddleware())
    if interrupt_on:
        # Last entry, so the policy gates keep their place among the
        # after-model hooks.
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=dict(interrupt_on)))
    return create_agent(
        chat_model,
        system_prompt=system_prompt,
        tools=list(tools),
        middleware=middleware,
        response_format=response_format,
        checkpointer=checkpointer,
        state_schema=KernelState,
    )


def result_call_names(response_format: Any) -> frozenset[str]:
    """Call names under which the framework delivers a typed final result."""
    if response_format is None:
        return frozenset()
    return frozenset(spec.name for spec in response_format.schema_specs)


def runtime_available() -> bool:
    """Whether the optional ``agent`` extra's agent runtime imports."""
    try:
        import langchain.agents  # noqa: F401
    except ImportError:
        return False
    return True
