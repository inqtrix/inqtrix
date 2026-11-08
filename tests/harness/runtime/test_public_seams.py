"""The public seams of LangChain and LangGraph the async kernel is going to stand on.

A small scripted model with ``bind_tools`` drives ``create_agent`` with the delta state
under ``astream``. Nothing of the kernel runs here: each test pins one library behaviour
its move to async relies on, so a library bump that changes the behaviour turns red here
first. The provider chain under ``astream`` is not part of this file. The last tests pin
how the kernel itself stands on these seams: its own graph, its reducer, and the rule
that a kernel thread takes input only while it is empty.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from contextvars import ContextVar
from pathlib import Path
from typing import Annotated, Any, Required

import pytest

pytest.importorskip("langchain")

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages
from langgraph.types import Command, interrupt
from pydantic import Field

_LIMIT_SECONDS = 20.0
_ACTOR: ContextVar[str] = ContextVar("probe_actor", default="none")
_BUDGET: ContextVar[str] = ContextVar("probe_budget", default="none")


def messages_reducer(state: list[AnyMessage] | None, writes: list[Any]) -> list[AnyMessage]:
    """Batch reducer of the delta channel; folds the public ``add_messages``."""
    merged = list(state or [])
    for write in writes:
        merged = add_messages(merged, write)
    return merged


class DeltaState(AgentState):
    messages: Required[
        Annotated[list[AnyMessage], DeltaChannel(messages_reducer, snapshot_frequency=50)]
    ]


class ScriptedModel(BaseChatModel):
    """Answers each call with its next turn, streamed word by word when asked to."""

    turns: list[AIMessage]
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedModel":
        return self

    def _next_turn(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.turns[len(self.calls) - 1]

    def _generate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_turn(messages))])

    async def _agenerate(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        return self._generate(messages)

    async def _astream(self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any) -> AsyncIterator[ChatGenerationChunk]:
        turn = self._next_turn(messages)
        words = str(turn.content).split(" ") if turn.content else []
        for index, word in enumerate(words):
            piece = word if index == len(words) - 1 else f"{word} "
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        calls = [
            {"name": call["name"], "args": _json(call["args"]), "id": call["id"], "index": index, "type": "tool_call_chunk"}
            for index, call in enumerate(turn.tool_calls)
        ]
        if calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=calls))


def _json(args: dict[str, Any]) -> str:
    import json

    return json.dumps(args)


def _call(name: str, call_id: str, **args: Any) -> dict[str, Any]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _tool_turn(*calls: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=list(calls))


def _agent(turns: list[AIMessage], tools: Sequence[Callable[..., Any]], *, middleware: Sequence[Any] = (), checkpointer: Any = None) -> Any:
    return create_agent(
        ScriptedModel(turns=turns),
        tools=list(tools),
        middleware=list(middleware),
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
        state_schema=DeltaState,
    )


def _config(thread: str = "seam") -> dict[str, Any]:
    return {"configurable": {"thread_id": thread}}


_QUESTION = {"messages": [HumanMessage(content="Frage")]}


async def _drain(graph: Any, payload: Any, config: dict[str, Any], **kwargs: Any) -> list[Any]:
    """Read a whole stream under ``aclosing``, as every consumer of the kernel will."""
    seen: list[Any] = []
    async with aclosing(graph.astream(payload, config, **kwargs)) as stream:
        async for chunk in stream:
            seen.append(chunk)
    return seen


async def _tool_replies(graph: Any, config: dict[str, Any]) -> list[tuple[str, str]]:
    snapshot = await graph.aget_state(config)
    return [
        (message.tool_call_id, str(message.content))
        for message in snapshot.values["messages"]
        if isinstance(message, ToolMessage)
    ]


def test_the_delta_state_wins_the_messages_channel() -> None:
    graph = _agent([AIMessage(content="fertig")], [])

    channel = graph.channels["messages"]

    assert isinstance(channel, DeltaChannel)
    assert channel.snapshot_frequency == 50


@pytest.mark.asyncio
async def test_astream_yields_updates_and_messages_together() -> None:
    @tool
    async def lookup() -> str:
        """Look up."""
        return "gefunden"

    graph = _agent([_tool_turn(_call("lookup", "c1")), AIMessage(content="Die Antwort")], [lookup])

    async with asyncio.timeout(_LIMIT_SECONDS):
        seen = await _drain(graph, _QUESTION, _config(), stream_mode=["updates", "messages"])

    nodes = [next(iter(chunk)) for mode, chunk in seen if mode == "updates"]
    assert nodes == ["model", "tools", "model"]
    words = [
        chunk[0].content
        for mode, chunk in seen
        if mode == "messages" and isinstance(chunk[0], AIMessageChunk) and chunk[0].content
    ]
    assert words == ["Die ", "Antwort"]


@pytest.mark.asyncio
async def test_parallel_tools_overlap_and_reply_in_call_order() -> None:
    """The first call finishes last; both only finish because they overlap."""
    first_started = asyncio.Event()
    second_done = asyncio.Event()

    @tool
    async def first() -> str:
        """Waits for the second call."""
        first_started.set()
        await second_done.wait()
        return "erster"

    @tool
    async def second() -> str:
        """Lets the first call go on."""
        await first_started.wait()
        second_done.set()
        return "zweiter"

    graph = _agent(
        [_tool_turn(_call("first", "c1"), _call("second", "c2")), AIMessage(content="fertig")],
        [first, second],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        replies = await _tool_replies(graph, _config())

    assert replies == [("c1", "erster"), ("c2", "zweiter")]


@pytest.mark.asyncio
async def test_a_parked_tool_resumes_by_id_after_a_reload_without_rerunning_its_sibling() -> None:
    """Parks, loads a fresh graph on the same saver, resumes; the order stays the calls'."""
    runs = {"ask_user": 0, "lookup": 0}

    @tool
    async def ask_user(question: str) -> str:
        """Asks the person."""
        runs["ask_user"] += 1
        return interrupt({"kind": "ask_user", "question": question})

    @tool
    async def lookup() -> str:
        """Looks something up."""
        runs["lookup"] += 1
        return "gefunden"

    saver = InMemorySaver()
    turns = [
        _tool_turn(_call("ask_user", "c1", question="Welche?"), _call("lookup", "c2")),
        AIMessage(content="fertig"),
    ]
    graph = _agent(turns, [ask_user, lookup], checkpointer=saver)

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        parked = await graph.aget_state(_config())
        assert [pending.value["kind"] for pending in parked.interrupts] == ["ask_user"]
        assert runs == {"ask_user": 1, "lookup": 1}

        reloaded = _agent(turns[1:], [ask_user, lookup], checkpointer=saver)
        await _drain(reloaded, Command(resume={parked.interrupts[0].id: "Diese"}), _config(), stream_mode="updates")
        replies = await _tool_replies(reloaded, _config())

    assert replies == [("c1", "Diese"), ("c2", "gefunden")]
    assert runs == {"ask_user": 2, "lookup": 1}


@pytest.mark.asyncio
async def test_two_parked_tools_each_get_their_own_answer() -> None:
    @tool
    async def ask_user(question: str) -> str:
        """Asks the person."""
        return interrupt({"question": question})

    graph = _agent(
        [
            _tool_turn(_call("ask_user", "c1", question="links"), _call("ask_user", "c2", question="rechts")),
            AIMessage(content="fertig"),
        ],
        [ask_user],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        parked = await graph.aget_state(_config())
        answers = {pending.id: f"Antwort {pending.value['question']}" for pending in parked.interrupts}
        assert len(answers) == 2
        await _drain(graph, Command(resume=answers), _config(), stream_mode="updates")
        replies = await _tool_replies(graph, _config())

    assert replies == [("c1", "Antwort links"), ("c2", "Antwort rechts")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "ran", "reply_is_error"),
    [("approve", ["Text"], False), ("reject", [], True)],
)
async def test_a_gated_call_runs_only_after_its_approval(decision: str, ran: list[str], reply_is_error: bool) -> None:
    published: list[str] = []

    @tool
    async def publish(text: str) -> str:
        """Publishes."""
        published.append(text)
        return "veröffentlicht"

    graph = _agent(
        [_tool_turn(_call("publish", "c1", text="Text")), AIMessage(content="fertig")],
        [publish],
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"publish": True})],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        parked = await graph.aget_state(_config())
        assert published == []
        assert parked.interrupts[0].value["action_requests"][0]["name"] == "publish"
        await _drain(
            graph,
            Command(resume={parked.interrupts[0].id: {"decisions": [{"type": decision}]}}),
            _config(),
            stream_mode="updates",
        )
        snapshot = await graph.aget_state(_config())

    assert published == ran
    reply = next(message for message in snapshot.values["messages"] if isinstance(message, ToolMessage))
    assert (reply.status == "error") is reply_is_error
    assert snapshot.interrupts == ()


@pytest.mark.asyncio
async def test_an_approval_and_a_question_park_one_after_the_other() -> None:
    published: list[str] = []

    @tool
    async def publish(text: str) -> str:
        """Publishes."""
        published.append(text)
        return "veröffentlicht"

    @tool
    async def ask_user(question: str) -> str:
        """Asks the person."""
        return interrupt({"kind": "ask_user", "question": question})

    graph = _agent(
        [
            _tool_turn(_call("publish", "c1", text="Text")),
            _tool_turn(_call("ask_user", "c2", question="Welche?")),
            AIMessage(content="fertig"),
        ],
        [publish, ask_user],
        middleware=[HumanInTheLoopMiddleware(interrupt_on={"publish": True})],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        gate = (await graph.aget_state(_config())).interrupts[0]
        await _drain(
            graph,
            Command(resume={gate.id: {"decisions": [{"type": "approve"}]}}),
            _config(),
            stream_mode="updates",
        )
        question = (await graph.aget_state(_config())).interrupts[0]
        assert question.value["kind"] == "ask_user"
        assert question.id != gate.id
        await _drain(graph, Command(resume={question.id: "Diese"}), _config(), stream_mode="updates")
        replies = await _tool_replies(graph, _config())

    assert published == ["Text"]
    assert replies == [("c1", "veröffentlicht"), ("c2", "Diese")]


@pytest.mark.asyncio
async def test_cancelling_the_task_reaches_the_tool_during_its_await() -> None:
    started = asyncio.Event()
    outcome: list[str] = []

    @tool
    async def wait_long() -> str:
        """Waits."""
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        return "nie"

    graph = _agent([_tool_turn(_call("wait_long", "c1")), AIMessage(content="fertig")], [wait_long])

    async with asyncio.timeout(_LIMIT_SECONDS):
        task = asyncio.create_task(_drain(graph, _QUESTION, _config(), stream_mode="updates"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert outcome == ["cancelled"]


@pytest.mark.asyncio
async def test_closing_the_stream_ends_a_tool_while_the_consumer_waits_outside_it() -> None:
    """The consumer holds an event, the tool still runs; leaving ``aclosing`` must end it."""
    outcome: list[str] = []
    release = asyncio.Event()

    @tool
    async def quick() -> str:
        """Returns at once, so the stream yields while the other call runs."""
        return "schnell"

    @tool
    async def wait_long() -> str:
        """Waits."""
        try:
            await release.wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise
        outcome.append("finished")
        return "spät"

    graph = _agent(
        [_tool_turn(_call("quick", "c1"), _call("wait_long", "c2")), AIMessage(content="fertig")],
        [quick, wait_long],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        try:
            async with aclosing(graph.astream(_QUESTION, _config(), stream_mode="updates")) as stream:
                async for chunk in stream:
                    if "tools" in chunk:
                        await asyncio.sleep(0.05)
                        assert outcome == []
                        break
            await asyncio.sleep(0)
            assert outcome == ["cancelled"]
        finally:
            release.set()


@pytest.mark.asyncio
async def test_parallel_tools_keep_their_own_context_and_see_the_callers() -> None:
    _BUDGET.set("budget-des-laufs")
    both_bound = asyncio.Barrier(2)

    async def _work(actor: str) -> str:
        _ACTOR.set(actor)
        await both_bound.wait()
        return f"{_ACTOR.get()}/{_BUDGET.get()}"

    @tool
    async def left() -> str:
        """Binds its actor."""
        return await _work("links")

    @tool
    async def right() -> str:
        """Binds its actor."""
        return await _work("rechts")

    graph = _agent(
        [_tool_turn(_call("left", "c1"), _call("right", "c2")), AIMessage(content="fertig")],
        [left, right],
    )

    async with asyncio.timeout(_LIMIT_SECONDS):
        await _drain(graph, _QUESTION, _config(), stream_mode="updates")
        replies = await _tool_replies(graph, _config())

    assert replies == [("c1", "links/budget-des-laufs"), ("c2", "rechts/budget-des-laufs")]
    assert _ACTOR.get() == "none"


@pytest.mark.asyncio
async def test_a_sync_tool_runs_in_the_default_executor_with_the_callers_context() -> None:
    _BUDGET.set("budget-des-laufs")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="seam-pool")
    asyncio.get_running_loop().set_default_executor(pool)

    @tool
    def blocking() -> str:
        """Runs on a thread."""
        return f"{threading.current_thread().name.rsplit('_', 1)[0]}/{_BUDGET.get()}"

    graph = _agent([_tool_turn(_call("blocking", "c1")), AIMessage(content="fertig")], [blocking])

    try:
        async with asyncio.timeout(_LIMIT_SECONDS):
            await _drain(graph, _QUESTION, _config(), stream_mode="updates")
            replies = await _tool_replies(graph, _config())
    finally:
        pool.shutdown(wait=False)

    assert replies == [("c1", "seam-pool/budget-des-laufs")]



# -- the kernel's own footing on these seams --------------------------------- #

_AGENT_PACKAGE = "deepagents"
"""The agent package the kernel was built on before it moved to ``create_agent``."""


def test_the_former_agent_package_stays_out_of_the_dependencies_and_the_lock() -> None:
    root = Path(__file__).resolve().parents[3]

    assert _AGENT_PACKAGE not in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert f'name = "{_AGENT_PACKAGE}"' not in (root / "uv.lock").read_text(encoding="utf-8")


def test_the_kernel_graph_holds_its_own_delta_state_with_the_public_reducer() -> None:
    from inqtrix.agents.harness import build_kernel_agent
    from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
    from inqtrix.agents.kernel.state import MESSAGES_SNAPSHOT_FREQUENCY, messages_reducer
    from tests.agents.test_harness_kernel_contract import _ScriptedProvider

    graph = build_kernel_agent(
        build_tool_chat_model(_ScriptedProvider([])),
        tools=[],
        system_prompt="Test.",
        max_tool_calls=1,
        context_keep_messages=4,
    )
    channel = graph.channels["messages"]

    assert isinstance(channel, DeltaChannel)
    assert channel.snapshot_frequency == MESSAGES_SNAPSHOT_FREQUENCY == 50
    assert channel.reducer is messages_reducer


def test_the_kernel_reducer_keeps_the_semantics_of_add_messages() -> None:
    """Merge by id and a full reset, the case LangGraph's private delta reducer misses."""
    from inqtrix.agents.kernel.state import messages_reducer

    first = HumanMessage(content="alt", id="m1")
    edited = HumanMessage(content="neu", id="m1")
    fresh = HumanMessage(content="frisch", id="m2")

    assert messages_reducer([first], [[edited]]) == [edited]
    assert messages_reducer([first], [[RemoveMessage(id=REMOVE_ALL_MESSAGES), fresh]]) == [fresh]


def test_a_kernel_thread_takes_input_only_while_it_is_empty() -> None:
    """Nothing writes into a running thread, so no dangling tool call needs repair."""
    from types import SimpleNamespace

    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm

    algorithm = SimpleNamespace(_resume_value=lambda run_id, existing: {"decisions": []})
    started = SimpleNamespace(values={"messages": [HumanMessage(content="Frage")]}, tasks=())
    parked = SimpleNamespace(
        values=started.values, tasks=(SimpleNamespace(interrupts=("frage",)),)
    )

    assert KernelAgentAlgorithm._graph_input(algorithm, None, "run", started, None) is None
    resumed = KernelAgentAlgorithm._graph_input(algorithm, None, "run", parked, None)
    assert isinstance(resumed, Command) and resumed.resume == {"decisions": []}
