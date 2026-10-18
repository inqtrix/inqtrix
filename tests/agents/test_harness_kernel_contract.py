"""deepagents contract tests for the kernel seam.

These tests ARE the deepagents upgrade gate: they freeze the exact
behavior the kernel runtime depends on — built-in tool exclusion via the
``inqtrix:kernel`` harness profile, the HITL interrupt payload and
resume-decision shapes, and park/resume across a REBUILT agent (a parked
run resumes in another worker process with a fresh compiled graph over
the shared checkpointer). A deepagents bump that changes any of these
must fail here before it can reach production.
"""

from __future__ import annotations

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from inqtrix.agents.harness import build_kernel_agent
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.agents.kernel.middleware import (
    KernelToolBudgetExceeded,
    KernelToolBudgetMiddleware,
)
from inqtrix.providers.base import ChatTurn, LLMProvider, ToolCallRequest


def test_deepagents_version_is_pinned_to_contracted_range():
    import importlib.metadata

    version = importlib.metadata.version("deepagents")
    parts = tuple(int(p) for p in version.split(".")[:3])
    assert (0, 7, 13) <= parts < (0, 8, 0), (
        f"deepagents {version} is outside the contracted >=0.7.13,<0.8 "
        "range — re-run these contract tests deliberately and update the "
        "pin, never bump silently."
    )


class _ScriptedProvider(LLMProvider):
    """Replays scripted ChatTurns; records every chat() call."""

    def __init__(self, turns: list[ChatTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[dict] = []

    def complete(self, prompt, **kwargs):  # type: ignore[override]
        raise AssertionError("kernel path must use chat(), not complete()")

    def is_available(self) -> bool:
        return True

    def supports_tool_calls(self, *, model=None) -> bool:
        return True

    def chat(self, messages, *, tools=None, model=None, **kwargs):  # type: ignore[override]
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._turns:
            raise AssertionError("scripted provider ran out of turns")
        return self._turns.pop(0)


def _text_turn(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        raw=None,
    )


def _tool_call_turn(call_id: str, name: str, arguments: dict) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=(
            ToolCallRequest(id=call_id, name=name, arguments=arguments),
        ),
        finish_reason="tool_calls",
        model="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        raw=None,
    )


def _tool_call_batch_turn(*calls: ToolCallRequest) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=tuple(calls),
        finish_reason="tool_calls",
        model="scripted",
        prompt_tokens=1,
        completion_tokens=1,
        raw=None,
    )


@tool
def echo_tool(text: str) -> str:
    """Gibt den Text zurueck."""
    return f"echo: {text}"


def _user_input(question: str) -> dict:
    return {"messages": [{"role": "user", "content": question}]}


def test_supersteps_per_tool_turn_is_pinned():
    """Derives ``_SUPERSTEPS_PER_TOOL_TURN`` by measuring the compiled graph.

    The kernel prices its recursion ceilings in TOOL TURNS times this
    constant, so the constant must track the real graph: a deepagents
    upgrade or a new kernel middleware that adds a node silently shrinks
    every published turn budget unless this measurement fails first. The
    probe finds the minimal ``recursion_limit`` that lets a run with N
    sequential tool calls plus a final answer finish, in the production
    graph shape (balanced policy variant -> HITL node present, budget and
    summarization middleware attached — the same slots the kernel's
    ``_compiled_graph`` fills).
    """
    from langgraph.errors import GraphRecursionError

    from inqtrix.agents.kernel.algorithm import (
        _ANSWER_TURN_SUPERSTEPS,
        _SCHNELL_READ_TURNS,
        _SCHNELL_TOOL_TURNS,
        _SUPERSTEPS_PER_TOOL_TURN,
    )
    from inqtrix.agents.kernel.policy import interrupt_config_for

    def _min_limit(tool_turns: int) -> int:
        for limit in range(1, 40):
            provider = _ScriptedProvider(
                [
                    _tool_call_turn(
                        f"call_step{index}", "echo_tool", {"text": str(index)}
                    )
                    for index in range(tool_turns)
                ]
                + [_text_turn("Fertig.")]
            )
            agent = build_kernel_agent(
                build_tool_chat_model(provider),
                tools=[echo_tool],
                system_prompt="Test.",
                # echo_tool itself is ungated in balanced — the probe
                # measures the per-turn node cost, not an interrupt.
                interrupt_on=interrupt_config_for("balanced"),
                checkpointer=MemorySaver(),
                max_tool_calls=30,
                context_keep_messages=20,
            )
            try:
                agent.invoke(
                    _user_input("Frage"),
                    config={
                        "recursion_limit": limit,
                        "configurable": {
                            "thread_id": f"steps-{tool_turns}-{limit}"
                        },
                    },
                )
            except GraphRecursionError:
                continue
            return limit
        pytest.fail(
            f"no recursion_limit up to 39 completed {tool_turns} tool turns"
        )

    answer_only = _min_limit(0)
    one_call = _min_limit(1)
    two_calls = _min_limit(2)
    assert answer_only == _ANSWER_TURN_SUPERSTEPS, (
        f"the bare answer turn costs {answer_only} super-steps but "
        f"_ANSWER_TURN_SUPERSTEPS says {_ANSWER_TURN_SUPERSTEPS} — every "
        "derived ceiling formula is off by the same amount."
    )
    measured_per_turn = two_calls - one_call
    assert measured_per_turn == _SUPERSTEPS_PER_TOOL_TURN, (
        f"one tool turn costs {measured_per_turn} super-steps but "
        f"_SUPERSTEPS_PER_TOOL_TURN says {_SUPERSTEPS_PER_TOOL_TURN} — "
        "a middleware/node change moved the price; update the constant "
        "(and with it every derived ceiling) deliberately, never guess."
    )
    # The schnell clamp must afford the tier's published web call PLUS a
    # knowledge/todo or failed-call turn plus the answer (its documented
    # affordance) — not merely the single cheapest happy path.
    schnell_clamp = (
        _ANSWER_TURN_SUPERSTEPS
        + _SUPERSTEPS_PER_TOOL_TURN
        * (_SCHNELL_TOOL_TURNS + _SCHNELL_READ_TURNS)
    )
    assert two_calls <= schnell_clamp, (
        f"a two-tool-call schnell run needs recursion_limit {two_calls} "
        f"but the clamp only buys {schnell_clamp} — the tier dies on an "
        "ordinary knowledge+web trajectory."
    )
    # ...which a literal clamp of 8 does not afford: 8 buys not even the
    # bare answer run.
    assert one_call > 8, (
        "a one-tool-call run fits into recursion_limit 8 — the arithmetic "
        "behind the schnell clamp has changed; re-derive the clamp instead "
        "of trusting it."
    )


def test_kernel_toolset_is_inqtrix_tools_plus_todos_only():
    provider = _ScriptedProvider([_text_turn("Fertig.")])
    agent = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
    )

    agent.invoke(_user_input("Hallo"))

    exposed = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    # Filesystem built-ins excluded via the inqtrix:kernel profile, the
    # general-purpose subagent (task tool) disabled, write_todos kept.
    assert exposed == {"echo_tool", "write_todos"}, (
        f"kernel tool surface drifted: {sorted(exposed)} — a deepagents "
        "upgrade added/renamed built-ins or profile resolution broke."
    )


def test_todos_survive_checkpoint_and_rebuilt_agent():
    todos = [{"content": "Check the source", "status": "in_progress"}]
    provider = _ScriptedProvider([
        _tool_call_turn("call_todos", "write_todos", {"todos": todos}),
        _text_turn("Task recorded."),
    ])
    saver = MemorySaver()
    config = {"configurable": {"thread_id": "todo-checkpoint"}}
    first = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
        checkpointer=saver,
        max_tool_calls=1,
    )
    first.invoke(_user_input("Track the task."), config=config)
    rebuilt = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([])),
        tools=[echo_tool],
        system_prompt="Test.",
        checkpointer=saver,
        max_tool_calls=1,
    )
    state = rebuilt.get_state(config).values
    assert state["todos"] == todos
    replies = [m for m in state["messages"] if m.type == "tool"]
    assert len(replies) == 1
    assert replies[0].tool_call_id == "call_todos"
    assert replies[0].status == "success"
    assert len(provider.calls) == 2


def test_excluded_filesystem_tool_cannot_execute(monkeypatch):
    from deepagents.backends import StateBackend

    executed = []

    def record_read(*args, **kwargs):
        executed.append((args, kwargs))
        return "Unauthorized filesystem content"

    monkeypatch.setattr(StateBackend, "read", record_read)
    provider = _ScriptedProvider([
        _tool_call_turn("call_excluded", "read_file", {"file_path": "/secret"}),
        _text_turn("Access refused."),
    ])
    agent = build_kernel_agent(
        build_tool_chat_model(provider), tools=[echo_tool], system_prompt="Test."
    )
    state = agent.invoke(_user_input("Read the file."))
    replies = [m for m in state["messages"] if m.type == "tool"]
    assert len(replies) == 1
    assert replies[0].tool_call_id == "call_excluded"
    assert replies[0].status == "error"
    assert replies[0].content == "Error: read_file is not available."
    assert executed == []


def test_kernel_tool_budget_allows_30_and_rejects_31() -> None:
    middleware = KernelToolBudgetMiddleware(30)
    allowed = AIMessage(
        content="",
        tool_calls=[
            {"id": f"call_{index}", "name": "echo_tool", "args": {}}
            for index in range(30)
        ],
    )
    assert middleware.after_model({"messages": [allowed]}, None) is None

    overflow = AIMessage(
        content="",
        tool_calls=[
            {"id": f"call_{index}", "name": "echo_tool", "args": {}}
            for index in range(31)
        ],
    )
    with pytest.raises(KernelToolBudgetExceeded) as raised:
        middleware.after_model({"messages": [overflow]}, None)
    assert raised.value.attempted == 31
    assert raised.value.batch_size == 31


def test_kernel_tool_budget_rejects_whole_overflowing_batch() -> None:
    executed: list[str] = []

    @tool
    def recording_tool(text: str) -> str:
        """Record execution for the batch-rejection contract."""
        executed.append(text)
        return text

    provider = _ScriptedProvider(
        [
            _tool_call_batch_turn(
                ToolCallRequest(
                    id="call_batch1",
                    name="recording_tool",
                    arguments={"text": "first"},
                ),
                ToolCallRequest(
                    id="call_batch2",
                    name="recording_tool",
                    arguments={"text": "second"},
                ),
            )
        ]
    )
    agent = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[recording_tool],
        system_prompt="Test.",
        max_tool_calls=1,
    )

    with pytest.raises(KernelToolBudgetExceeded):
        agent.invoke(_user_input("Run batch"))
    assert executed == []


def test_hitl_interrupt_payload_shape_is_frozen():
    provider = _ScriptedProvider(
        [
            _tool_call_turn("call_gate1", "echo_tool", {"text": "hi"}),
            _text_turn("Fertig."),
        ]
    )
    agent = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "contract-payload"}}

    interrupts = [
        item
        for update in agent.stream(
            _user_input("Frage"), config=config, stream_mode="updates"
        )
        if "__interrupt__" in update
        for item in update["__interrupt__"]
    ]

    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert set(payload.keys()) >= {"action_requests", "review_configs"}
    request = payload["action_requests"][0]
    assert request["name"] == "echo_tool"
    assert request["args"] == {"text": "hi"}
    review = payload["review_configs"][0]
    assert review["action_name"] == "echo_tool"
    assert review["allowed_decisions"] == [
        "approve",
        "edit",
        "reject",
        "respond",
    ]


def test_resume_approve_across_rebuilt_agent_executes_tool():
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "contract-resume"}}
    provider = _ScriptedProvider(
        [_tool_call_turn("call_gate2", "echo_tool", {"text": "weiter"})]
    )
    first = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=checkpointer,
        max_tool_calls=1,
    )
    parked = any(
        "__interrupt__" in update
        for update in first.stream(
            _user_input("Frage"), config=config, stream_mode="updates"
        )
    )
    assert parked

    # A parked run resumes in ANOTHER worker: fresh compiled graph over
    # the shared checkpointer, resume decisions rebuilt from control rows.
    resume_provider = _ScriptedProvider([_text_turn("Erledigt.")])
    second = build_kernel_agent(
        build_tool_chat_model(resume_provider),
        tools=[echo_tool],
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=checkpointer,
        max_tool_calls=1,
    )
    updates = list(
        second.stream(
            Command(resume={"decisions": [{"type": "approve"}]}),
            config=config,
            stream_mode="updates",
        )
    )

    assert not any("__interrupt__" in u for u in updates)
    state = second.get_state(config)
    messages = state.values["messages"]
    tool_results = [
        m for m in messages if getattr(m, "type", "") == "tool"
    ]
    assert any("echo: weiter" in str(m.content) for m in tool_results)
    final = messages[-1]
    assert isinstance(final, AIMessage)
    assert final.content == "Erledigt."


def test_interrupt_id_is_stable_across_reentry_and_rebuild():
    """The kernel derives its tool-approval row ids from interrupt ids.

    A crash between park and decision re-enters the graph and must find
    the SAME interrupt id (idempotent approval create) — as must a
    resume through a rebuilt agent in another worker. An upstream
    change to id derivation breaks park/resume idempotency and must
    fail here.
    """
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "contract-intr-id"}}
    provider = _ScriptedProvider(
        [_tool_call_turn("call_gate9", "echo_tool", {"text": "x"})]
    )
    first = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=checkpointer,
    )
    first_ids = [
        item.id
        for update in first.stream(
            _user_input("Frage"), config=config, stream_mode="updates"
        )
        if "__interrupt__" in update
        for item in update["__interrupt__"]
    ]

    rebuilt = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([])),
        tools=[echo_tool],
        system_prompt="Test.",
        interrupt_on={"echo_tool": True},
        checkpointer=checkpointer,
    )
    # Crash re-entry: stream(None) replays to the same pending gate.
    reentry_ids = [
        item.id
        for update in rebuilt.stream(
            None, config=config, stream_mode="updates"
        )
        if "__interrupt__" in update
        for item in update["__interrupt__"]
    ]
    state_ids = [
        item.id
        for task in rebuilt.get_state(config).tasks
        for item in task.interrupts
    ]
    assert first_ids and first_ids == reentry_ids == state_ids


def test_resume_reject_skips_tool_and_continues():
    checkpointer = MemorySaver()
    config = {"configurable": {"thread_id": "contract-reject"}}
    executed: list[str] = []

    @tool
    def gated_tool(text: str) -> str:
        """Aufzeichnendes Tool."""
        executed.append(text)
        return "ran"

    provider = _ScriptedProvider(
        [
            _tool_call_turn("call_gate3", "gated_tool", {"text": "nein"}),
            _text_turn("Verstanden, ohne Tool beantwortet."),
        ]
    )
    agent = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[gated_tool],
        system_prompt="Test.",
        interrupt_on={"gated_tool": True},
        checkpointer=checkpointer,
    )
    assert any(
        "__interrupt__" in update
        for update in agent.stream(
            _user_input("Frage"), config=config, stream_mode="updates"
        )
    )

    updates = list(
        agent.stream(
            Command(resume={"decisions": [{"type": "reject"}]}),
            config=config,
            stream_mode="updates",
        )
    )

    assert not any("__interrupt__" in u for u in updates)
    assert executed == []
    final = agent.get_state(config).values["messages"][-1]
    assert final.content == "Verstanden, ohne Tool beantwortet."


def test_summarization_replacement_contract():
    """Upgrade gate: the kernel REPLACES the base-stack summarization.

    Three pinned facts, each of which a deepagents upgrade could silently
    break: (1) the kernel harness profile excludes the base summarization
    CLASS (exact-type matching preserves subclasses); (2) the kernel's
    subclass reports its own ``.name`` — the string alias
    ``"SummarizationMiddleware"`` must NOT match it, or the profile would
    strip our replacement too; (3) the subclass keeps the seams the
    kernel overrides (a rename upstream must fail here, not in
    production).
    """
    from deepagents.middleware.summarization import (
        _DeepAgentsSummarizationMiddleware,
    )

    from inqtrix.agents.harness import (
        _kernel_summarization_middleware_cls,
        _register_kernel_harness_profile,
    )

    _register_kernel_harness_profile()
    from deepagents.profiles.harness.harness_profiles import (
        _get_harness_profile,
    )

    from inqtrix.agents.kernel.chat_bridge import (
        KERNEL_MODEL_IDENTIFIER,
        KERNEL_MODEL_PROVIDER,
    )

    profile = _get_harness_profile(
        f"{KERNEL_MODEL_PROVIDER}:{KERNEL_MODEL_IDENTIFIER}"
    )
    assert _DeepAgentsSummarizationMiddleware in profile.excluded_middleware

    cls = _kernel_summarization_middleware_cls()
    assert issubclass(cls, _DeepAgentsSummarizationMiddleware)
    # The subclass must not inherit the public alias (string exclusions
    # match `.name`): deepagents documents the fallback to __name__.
    assert cls.__name__ == "KernelSummarizationMiddleware"
    for seam in (
        "_should_summarize",
        "_offload_to_backend",
        "_aoffload_to_backend",
        "_build_new_messages_with_path",
        "wrap_model_call",
        "awrap_model_call",
    ):
        assert seam in vars(cls), f"overridden seam {seam} vanished"
        assert hasattr(_DeepAgentsSummarizationMiddleware, seam), (
            f"upstream seam {seam} renamed — the kernel override is dead"
        )


def test_compiled_graph_uses_one_kernel_summarization(monkeypatch):
    from deepagents.middleware.summarization import (
        _DeepAgentsSummarizationMiddleware,
    )

    entered = []
    original = _DeepAgentsSummarizationMiddleware.wrap_model_call

    def observe(middleware, request, handler):
        entered.append(type(middleware).__name__)
        return original(middleware, request, handler)

    monkeypatch.setattr(_DeepAgentsSummarizationMiddleware, "wrap_model_call", observe)
    agent = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([_text_turn("Done.")])),
        tools=[echo_tool],
        system_prompt="Test.",
        context_keep_messages=1,
    )
    agent.invoke(_user_input("Answer."))
    assert entered == ["KernelSummarizationMiddleware"]
