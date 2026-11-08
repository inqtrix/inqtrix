"""Contract tests for the kernel seam on LangChain's agent loop.

These tests ARE the upgrade gate of the agent framework: they freeze the
exact behavior the kernel runtime depends on — a tool surface of Inqtrix
tools and the planner only, the HITL interrupt payload and resume-decision
shapes, and park/resume across a REBUILT agent (a parked run resumes in
another worker process with a fresh compiled graph over the shared
checkpointer). A framework bump that changes any of these must fail here
before it can reach production.
"""

from __future__ import annotations

import pytest

pytest.importorskip("langchain")

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
    constant, so the constant must track the real graph: a framework
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
    # A named document costs more turns than a search: the todo list, one
    # search, four windows of 20,000 characters, citing and the canvas came to
    # seven tool turns in a measured run, and the answer has to fit after them.
    document_task = answer_only + 7 * measured_per_turn
    assert document_task <= schnell_clamp, (
        f"a document task needs recursion_limit {document_task} but the clamp "
        f"only buys {schnell_clamp}: it stops with the memo written and no answer."
    )
    # ...which a literal clamp of 8 does not afford: 8 buys not even the
    # bare answer run.
    assert one_call > 8, (
        "a one-tool-call run fits into recursion_limit 8 — the arithmetic "
        "behind the schnell clamp has changed; re-derive the clamp instead "
        "of trusting it."
    )


def test_the_schnell_tier_stays_bounded_at_twelve_model_calls():
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import configured_kernel_limits

    platform = SimpleNamespace(
        kernel_max_tool_calls=30,
        kernel_max_iterations=120,
        kernel_max_tool_calls_extension_ceiling=60,
        kernel_max_iterations_extension_ceiling=240,
    )

    tool_base, tool_ceiling, step_base, step_ceiling = configured_kernel_limits(
        platform, depth="normal", tier="schnell"
    )

    # Eleven tool turns and the answer, at eight steps each. No extension.
    assert (step_base, step_ceiling) == (96, 96)
    assert (tool_base, tool_ceiling) == (30, 30)


def test_a_caller_without_its_own_limit_gets_the_generous_default():
    """A kernel run prices its own ceiling. Any other caller relies on the 9 999
    steps ``create_agent`` sets; LangGraph's own 25 would stop it after three turns."""
    agent = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([])), tools=[echo_tool], system_prompt="Test."
    )

    assert agent.config["recursion_limit"] == 9_999


def test_kernel_toolset_is_inqtrix_tools_plus_todos_only():
    provider = _ScriptedProvider([_text_turn("Fertig.")])
    agent = build_kernel_agent(
        build_tool_chat_model(provider),
        tools=[echo_tool],
        system_prompt="Test.",
    )

    agent.invoke(_user_input("Hallo"))

    exposed = {t["function"]["name"] for t in provider.calls[0]["tools"]}
    # The kernel's own tools and the planner, nothing a framework adds.
    assert exposed == {"echo_tool", "write_todos"}, (
        f"kernel tool surface drifted: {sorted(exposed)} — a framework "
        "upgrade or a middleware added tools the kernel does not vet."
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


def test_a_tool_the_kernel_does_not_offer_cannot_execute():
    """A call to a filesystem tool, or any other unoffered name, ends as an error result."""
    provider = _ScriptedProvider([
        _tool_call_turn("call_unoffered", "read_file", {"file_path": "/secret"}),
        _text_turn("Access refused."),
    ])
    agent = build_kernel_agent(
        build_tool_chat_model(provider), tools=[echo_tool], system_prompt="Test."
    )
    state = agent.invoke(_user_input("Read the file."))
    replies = [m for m in state["messages"] if m.type == "tool"]
    assert len(replies) == 1
    assert replies[0].tool_call_id == "call_unoffered"
    assert replies[0].status == "error"
    assert "read_file is not a valid tool" in replies[0].content
    assert "Unauthorized" not in replies[0].content


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


def test_the_kernel_compacts_with_its_own_middleware_only():
    """The base-stack summarization is gone; the kernel's own compaction stays.

    The base would leave its state channel in the graph.
    """
    from inqtrix.agents.kernel.compaction import COMPACTION_STATE

    agent = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([_text_turn("Done.")])),
        tools=[echo_tool],
        system_prompt="Test.",
        context_keep_messages=1,
    )

    assert COMPACTION_STATE in agent.channels
    assert "_summarization_event" not in agent.channels
    agent.invoke(_user_input("Answer."))
