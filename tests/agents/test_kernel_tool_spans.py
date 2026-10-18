"""Kernel tool observations: one ``execute_tool <name>`` span per executed call.

The kernel's tool-call middleware wraps every call its tool node executes in
a span that a timeline row finds by its call id. These tests pin what that
span records (identity, outcome, content only under the capture policy), what
it never records (exception prose, interrupt payloads, tool artifacts), that
it never changes the call it observes, and where it sits in the composed
middleware stack.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("deepagents")

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt, ParentCommand
from langgraph.types import Command, Interrupt

from inqtrix.agents.harness import build_kernel_agent
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded
from inqtrix.observability import content as content_module
from inqtrix.observability import otel as otel_module
from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.observability.otel import operation_span
from inqtrix.providers.base import ChatTurn, ToolCallRequest
from tests._tracing_helpers import (
    ProxyTrace,
    everything_a_span_exports,
    in_memory_provider,
)
from tests.agents.test_harness_kernel_contract import (
    _ScriptedProvider,
    _text_turn,
    _tool_call_batch_turn,
    _tool_call_turn,
    echo_tool,
)


# -- shared helpers ---------------------------------------------------------- #


def _span_middleware() -> Any:
    from inqtrix.agents.kernel.middleware import KernelToolSpanMiddleware

    return KernelToolSpanMiddleware()


def _capture(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """Publish the content policy explicitly; never the env fallback."""
    monkeypatch.setattr(
        content_module,
        "_active_policy",
        ContentCapturePolicy(configured_capture=on, max_attr_bytes=32_768),
    )


def _request(
    name: str = "web_instant",
    args: dict[str, Any] | None = None,
    call_id: str | None = "c1",
    *,
    registered: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": dict(args or {}), "id": call_id},
        tool=object() if registered else None,
    )


def _tool_spans(exporter: Any) -> list[Any]:
    return [
        span
        for span in exporter.get_finished_spans()
        if (span.attributes or {}).get("gen_ai.operation.name") == "execute_tool"
    ]


def _exception_events(span: Any) -> list[Any]:
    return [event for event in span.events if event.name == "exception"]


def _production_kernel(provider: Any, tools: list[Any]) -> Any:
    """The compiled kernel graph in the shape ``_compiled_graph`` builds."""
    from inqtrix.agents.kernel.policy import interrupt_config_for

    return build_kernel_agent(
        build_tool_chat_model(provider),
        tools=tools,
        system_prompt="Test.",
        interrupt_on=interrupt_config_for("balanced"),
        checkpointer=MemorySaver(),
        max_tool_calls=30,
        context_keep_messages=20,
    )


def _user_input(question: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": question}]}


def _wait_for(condition: Callable[[], Any], *, timeout: float = 5.0) -> Any:
    """Spans end on their own threads; poll briefly instead of racing them."""
    deadline = time.time() + timeout
    while True:
        value = condition()
        if value or time.time() >= deadline:
            return value
        time.sleep(0.02)


def _ancestor_run(span: Any, by_id: dict[int, Any]) -> Any:
    current = span
    while current is not None and current.parent is not None:
        current = by_id.get(current.parent.span_id)
        if current is not None and current.name == "inqtrix.run":
            return current
    return None


@tool
def ask_probe(question: str) -> str:
    """Fragt den Nutzer."""
    from langgraph.types import interrupt

    answer = interrupt({"question": "MARKER-Q"})
    return f"answer: {answer}"


# -- the compiled kernel graph ---------------------------------------------- #


def test_compiled_kernel_graph_spans_every_executed_call_including_write_todos(
    recording, monkeypatch
):
    """Every call the kernel's tool node executes gets one tool observation
    named after the executed tool and keyed by its call id, the built-in
    ``write_todos`` included. Its output is the ToolMessage text."""
    _capture(monkeypatch, on=True)
    provider = _ScriptedProvider(
        [
            _tool_call_batch_turn(
                ToolCallRequest(id="c_echo", name="echo_tool", arguments={"text": "hallo"}),
                ToolCallRequest(
                    id="c_todo",
                    name="write_todos",
                    arguments={
                        "todos": [{"content": "MARKER-TODO", "status": "in_progress"}]
                    },
                ),
            ),
            _text_turn("Fertig."),
        ]
    )
    agent = _production_kernel(provider, [echo_tool])
    with operation_span("test.root") as root:
        agent.invoke(
            _user_input("Frage"),
            config={"configurable": {"thread_id": "tool-spans-todo"}},
        )

    spans = _tool_spans(recording)
    assert len(spans) == 2
    assert {
        (
            span.name,
            span.attributes["gen_ai.tool.name"],
            span.attributes["gen_ai.tool.call.id"],
        )
        for span in spans
    } == {
        ("execute_tool echo_tool", "echo_tool", "c_echo"),
        ("execute_tool write_todos", "write_todos", "c_todo"),
    }
    for span in spans:
        assert span.attributes["gen_ai.tool.type"] == "function"
        assert span.attributes["inqtrix.outcome"] == "completed"
        assert span.parent.span_id == root.get_span_context().span_id
    (todo,) = [s for s in spans if s.attributes["gen_ai.tool.call.id"] == "c_todo"]
    output = json.loads(todo.attributes["langfuse.observation.output"])
    assert isinstance(output, str)
    # The ToolMessage text itself, not a rendering of the Command around it.
    assert output.startswith("Updated todo list to ")
    assert "MARKER-TODO" in output


def test_interrupting_tool_parks_its_span_and_resume_completes_it(
    recording, monkeypatch
):
    """An interrupt is a park, not a failure: the first execution ends
    ``parked`` without an exception event or the interrupt payload, the
    resumed execution ends terminal under the same call id, and a sibling of
    the same model turn runs exactly once."""
    _capture(monkeypatch, on=False)
    provider = _ScriptedProvider(
        [
            _tool_call_batch_turn(
                ToolCallRequest(id="c_ask", name="ask_probe", arguments={"question": "?"}),
                ToolCallRequest(id="c_echo", name="echo_tool", arguments={"text": "sib"}),
            ),
            _text_turn("Fertig."),
        ]
    )
    agent = _production_kernel(provider, [echo_tool, ask_probe])
    config = {"configurable": {"thread_id": "tool-spans-park"}}
    parked_state = agent.invoke(_user_input("Frage"), config=config)
    assert parked_state.get("__interrupt__")
    agent.invoke(Command(resume="ok"), config=config)

    spans = _tool_spans(recording)
    asked = sorted(
        (s for s in spans if s.attributes.get("gen_ai.tool.call.id") == "c_ask"),
        key=lambda s: s.end_time,
    )
    assert [s.attributes.get("inqtrix.outcome") for s in asked] == [
        "parked",
        "completed",
    ]
    parked = asked[0]
    assert parked.status.status_code.name == "UNSET"
    assert _exception_events(parked) == []
    assert "MARKER-Q" not in everything_a_span_exports(parked)
    siblings = [s for s in spans if s.attributes.get("gen_ai.tool.call.id") == "c_echo"]
    assert len(siblings) == 1
    assert siblings[0].attributes["inqtrix.outcome"] == "completed"


def test_tool_span_middleware_is_the_outermost_result_changing_tool_layer(
    recording, monkeypatch
):
    """The span wraps every layer that can change a result: it is the first
    kernel-supplied middleware, the only tool layer outside it is the
    filesystem pass-through (eviction off), and the object that leaves the
    composed chain is the one the span observed. It adds no graph node."""
    import deepagents.graph as deepagents_graph
    import langchain.agents.factory as agent_factory
    from langchain.agents.middleware import AgentMiddleware

    _capture(monkeypatch, on=False)
    captured: dict[str, list[Any]] = {}
    real_create_agent = deepagents_graph.create_agent

    def spy_create_agent(*args: Any, **kwargs: Any) -> Any:
        captured["middleware"] = list(kwargs["middleware"])
        return real_create_agent(*args, **kwargs)

    monkeypatch.setattr(deepagents_graph, "create_agent", spy_create_agent)
    _production_kernel(_ScriptedProvider([]), [echo_tool])

    stack = captured["middleware"]
    names = [type(m).__name__ for m in stack]
    tool_layers = [
        type(m).__name__
        for m in stack
        if type(m).wrap_tool_call is not AgentMiddleware.wrap_tool_call
        or type(m).awrap_tool_call is not AgentMiddleware.awrap_tool_call
    ]
    assert tool_layers == [
        "FilesystemMiddleware",
        "KernelToolSpanMiddleware",
        "_ToolExclusionMiddleware",
    ]
    assert names.index("KernelToolSpanMiddleware") == (
        names.index("PatchToolCallsMiddleware") + 1
    )
    (filesystem,) = [m for m in stack if type(m).__name__ == "FilesystemMiddleware"]
    assert filesystem._tool_token_limit_before_evict is None
    span_cls = type(stack[names.index("KernelToolSpanMiddleware")])
    for hook in (
        "before_agent",
        "before_model",
        "after_model",
        "after_agent",
        "abefore_agent",
        "abefore_model",
        "aafter_model",
        "aafter_agent",
    ):
        assert getattr(span_cls, hook) is getattr(AgentMiddleware, hook), hook

    observed: list[Any] = []
    original_wrap = span_cls.wrap_tool_call

    def spy_wrap(self: Any, request: Any, handler: Any) -> Any:
        result = original_wrap(self, request, handler)
        observed.append(result)
        return result

    monkeypatch.setattr(span_cls, "wrap_tool_call", spy_wrap)
    left_chain: list[Any] = []
    real_chain = agent_factory._chain_tool_call_wrappers

    def spy_chain(wrappers: Any) -> Any:
        composed = real_chain(wrappers)

        def recorded(request: Any, execute: Any) -> Any:
            result = composed(request, execute)
            left_chain.append(result)
            return result

        return recorded

    monkeypatch.setattr(agent_factory, "_chain_tool_call_wrappers", spy_chain)
    provider = _ScriptedProvider(
        [
            _tool_call_turn("c_excl", "read_file", {"file_path": "/secret"}),
            _text_turn("Zugriff verweigert."),
        ]
    )
    agent = _production_kernel(provider, [echo_tool])
    agent.invoke(
        _user_input("Lies die Datei."),
        config={"configurable": {"thread_id": "tool-spans-order"}},
    )

    assert len(observed) == len(left_chain) == 1
    assert left_chain[0] is observed[0]
    (span,) = _tool_spans(recording)
    assert span.name == "execute_tool read_file"
    assert span.attributes["inqtrix.outcome"] == "failed"
    assert span.attributes["langfuse.observation.level"] == "WARNING"
    assert span.attributes["langfuse.observation.status_message"] == "error_status"


# -- the middleware contract ------------------------------------------------- #


def test_capture_off_keeps_args_result_artifact_and_exception_text_out_of_the_span(
    recording, monkeypatch
):
    """Without content capture the span carries identity and outcome only:
    no arguments, no result text, never the tool artifact, no exception
    prose, no interrupt payload, and no tool name the model invented. With
    capture on, input and output appear; artifact, credential, exception
    text and interrupt payload still do not."""
    from opentelemetry.trace import StatusCode

    markers = (
        "MARKER-ARGS",
        "MARKER-RESULT",
        "MARKER-ARTIFACT",
        "SECRET-KEY",
        "MARKER-EXC",
        "MARKER-QUESTION",
        "MARKER-NAME",
    )
    request = _request(
        args={"query": "MARKER-ARGS https://x.example?api_key=SECRET-KEY"}
    )
    exc = RuntimeError("MARKER-EXC")
    interrupt = GraphInterrupt([Interrupt(value="MARKER-QUESTION")])
    unknown = _request("MARKER-NAME", {"q": "x"}, "c2", registered=False)
    invented = ToolMessage(
        "Error: MARKER-NAME is not a valid tool, try one of [web_instant].",
        tool_call_id="c2",
        status="error",
    )

    def returns(request: Any) -> Any:
        return ToolMessage(
            "MARKER-RESULT", tool_call_id="c1", artifact={"r": "MARKER-ARTIFACT"}
        )

    def fails(request: Any) -> Any:
        raise exc

    def parks(request: Any) -> Any:
        raise interrupt

    def drive() -> None:
        middleware = _span_middleware()
        middleware.wrap_tool_call(request, returns)
        with pytest.raises(RuntimeError) as failed:
            middleware.wrap_tool_call(request, fails)
        assert failed.value is exc
        with pytest.raises(GraphInterrupt) as parked:
            middleware.wrap_tool_call(request, parks)
        assert parked.value is interrupt
        middleware.wrap_tool_call(unknown, lambda request: invented)

    _capture(monkeypatch, on=False)
    drive()
    result, error, park, unknown_span = _tool_spans(recording)
    for span in (result, error, park, unknown_span):
        exported = everything_a_span_exports(span)
        for marker in markers:
            assert marker not in exported, (marker, span.name)
    assert error.status.status_code is StatusCode.ERROR
    assert error.status.description == "RuntimeError"
    assert error.attributes["error.type"] == "RuntimeError"
    assert error.attributes["inqtrix.outcome"] == "error"
    (event,) = _exception_events(error)
    assert event.attributes["exception.message"] == "RuntimeError"
    assert park.status.status_code is StatusCode.UNSET
    assert _exception_events(park) == []
    assert unknown_span.name == "execute_tool unknown_tool"
    assert unknown_span.attributes["gen_ai.tool.name"] == "unknown_tool"
    assert unknown_span.attributes["gen_ai.tool.call.id"] == "c2"

    recording.clear()
    _capture(monkeypatch, on=True)
    drive()
    result, error, park, unknown_span = _tool_spans(recording)
    assert "MARKER-ARGS" in result.attributes["langfuse.observation.input"]
    assert "MARKER-RESULT" in result.attributes["langfuse.observation.output"]
    assert "MARKER-NAME" in unknown_span.attributes["langfuse.observation.input"]
    assert unknown_span.name == "execute_tool unknown_tool"
    for span in (result, error, park, unknown_span):
        exported = everything_a_span_exports(span)
        for marker in ("MARKER-ARTIFACT", "SECRET-KEY", "MARKER-EXC", "MARKER-QUESTION"):
            assert marker not in exported, (marker, span.name)


def test_cancel_and_token_budget_stops_are_distinct_and_never_error(
    recording, monkeypatch
):
    """A token-budget stop is a cancellation subclass, but the span keeps
    them apart: a cancel is ``cancelled`` without a level, a budget stop is
    ``budget_exceeded`` at WARNING. Neither sets ERROR or records the
    exception, and both leave as the identical object."""
    from opentelemetry.trace import StatusCode

    _capture(monkeypatch, on=False)
    cancel = AgentCancelled("MARKER-C")
    budget = AgentTokenBudgetExceeded("MARKER-B", usage={"prompt_tokens": 1})
    middleware = _span_middleware()
    for stop in (cancel, budget):

        def raises(request: Any, stop: Exception = stop) -> Any:
            raise stop

        with pytest.raises(AgentCancelled) as excinfo:
            middleware.wrap_tool_call(_request(), raises)
        assert excinfo.value is stop

    cancelled, exceeded = _tool_spans(recording)
    assert cancelled.attributes["inqtrix.outcome"] == "cancelled"
    assert "langfuse.observation.level" not in cancelled.attributes
    assert exceeded.attributes["inqtrix.outcome"] == "budget_exceeded"
    assert exceeded.attributes["langfuse.observation.level"] == "WARNING"
    assert (
        exceeded.attributes["langfuse.observation.status_message"]
        == "token_budget_exceeded"
    )
    for span in (cancelled, exceeded):
        assert span.status.status_code is StatusCode.UNSET
        assert _exception_events(span) == []
        exported = everything_a_span_exports(span)
        assert "MARKER-C" not in exported
        assert "MARKER-B" not in exported


def test_a_call_without_an_id_records_no_id_attribute(recording, monkeypatch):
    """A call without an id (the repo's providers always synthesize one)
    leaves the id attribute out instead of exporting the text 'None'."""
    _capture(monkeypatch, on=False)
    result = ToolMessage("ok", tool_call_id="")
    assert _span_middleware().wrap_tool_call(_request(call_id=None), lambda request: result) is result
    (span,) = _tool_spans(recording)
    assert "gen_ai.tool.call.id" not in span.attributes
    assert span.attributes["gen_ai.tool.name"] == "web_instant"


def test_visible_failures_are_warnings_not_errors(recording, monkeypatch):
    """A failure the model sees and the run survives is a WARNING on the
    tool observation, never ERROR: a failure prefix (also inside content
    blocks or a Command) or an error-status ToolMessage. The status message
    is the constant that classified it, never the tool's text."""
    from opentelemetry.trace import StatusCode

    _capture(monkeypatch, on=False)
    cases = [
        (
            ToolMessage("Tool blocked: MARKER-F", tool_call_id="c1", status="success"),
            "Tool blocked:",
        ),
        (
            ToolMessage(
                "Error: read_file is not available.", tool_call_id="c1", status="error"
            ),
            "error_status",
        ),
        # The context tools' ToolException carries both signals; the prefix
        # names the failure.
        (
            ToolMessage("Tool blocked: MARKER-F", tool_call_id="c1", status="error"),
            "Tool blocked:",
        ),
        (
            Command(
                update={
                    "messages": [ToolMessage("Tool unavailable: x", tool_call_id="c1")]
                }
            ),
            "Tool unavailable:",
        ),
        (
            ToolMessage(
                [{"type": "text", "text": "Tool error (x): MARKER-F"}],
                tool_call_id="c1",
            ),
            "Tool error",
        ),
    ]
    middleware = _span_middleware()
    for result, _ in cases:
        assert middleware.wrap_tool_call(_request(), lambda request, r=result: r) is result

    spans = _tool_spans(recording)
    assert [s.attributes["langfuse.observation.status_message"] for s in spans] == [
        expected for _, expected in cases
    ]
    for span in spans:
        assert span.attributes["inqtrix.outcome"] == "failed"
        assert span.attributes["langfuse.observation.level"] == "WARNING"
        assert span.status.status_code is StatusCode.UNSET
        assert "MARKER-F" not in everything_a_span_exports(span)


def test_result_shapes_are_read_and_an_unreadable_result_is_said(
    recording, monkeypatch, caplog
):
    """The ToolMessage of the call is found directly, in a Command's update
    (dict entries included, which the tool node does not convert) and inside
    a list result. A result without one is not reported as completed: the
    span says it could not read the result, and one WARNING says so."""
    from inqtrix.agents.kernel import middleware as middleware_module

    _capture(monkeypatch, on=False)
    monkeypatch.setattr(middleware_module, "_tool_span_warned", set())
    as_dict = Command(
        update={
            "messages": [
                {"role": "tool", "content": "Tool blocked: dict", "tool_call_id": "c1"}
            ]
        }
    )
    in_list = [Command(update={"messages": [ToolMessage("ok", tool_call_id="c1")]})]
    foreign = ToolMessage("ok", tool_call_id="another-call")
    no_messages = Command(update={"todos": []})
    middleware = _span_middleware()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        for result in (as_dict, in_list, foreign, no_messages):
            returned = middleware.wrap_tool_call(_request(), lambda request, r=result: r)
            assert returned is result

    dict_span, list_span, foreign_span, empty_span = _tool_spans(recording)
    assert dict_span.attributes["inqtrix.outcome"] == "failed"
    assert dict_span.attributes["langfuse.observation.status_message"] == "Tool blocked:"
    assert list_span.attributes["inqtrix.outcome"] == "completed"
    for span in (foreign_span, empty_span):
        assert "inqtrix.outcome" not in span.attributes
        assert span.attributes["inqtrix.tool.result_unreadable"] is True
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("Tool span could not read")
    ]
    assert len(warnings) == 1


def test_a_non_interrupt_bubble_up_passes_without_an_outcome(
    recording, monkeypatch, caplog
):
    """Only an interrupt parks. Any other graph bubble-up (a parent command,
    a drain) passes untouched and leaves the span without an outcome, and
    one WARNING says the observation has none."""
    from opentelemetry.trace import StatusCode

    from inqtrix.agents.kernel import middleware as middleware_module

    _capture(monkeypatch, on=False)
    monkeypatch.setattr(middleware_module, "_tool_span_warned", set())
    bubble = ParentCommand(Command(goto="elsewhere"))

    def raises(request: Any) -> Any:
        raise bubble

    middleware = _span_middleware()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        for _ in range(2):
            with pytest.raises(ParentCommand) as excinfo:
                middleware.wrap_tool_call(_request(), raises)
            assert excinfo.value is bubble

    for span in _tool_spans(recording):
        assert "inqtrix.outcome" not in span.attributes
        assert span.status.status_code is StatusCode.UNSET
        assert _exception_events(span) == []
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("Tool span recorded no outcome")
    ]
    assert len(warnings) == 1


@pytest.mark.parametrize("tracing", ["no_extra", "always_off"])
def test_untraced_tool_call_is_a_pure_passthrough_and_never_reads_the_policy(
    monkeypatch, caplog, tracing
):
    """Without a recording span the middleware only forwards the call: it
    reads no content policy (whose fallback would build settings from the
    environment), builds no content and logs nothing."""
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    if tracing == "no_extra":
        monkeypatch.setattr(otel_module, "_otel_trace", None)
    else:
        provider, _ = in_memory_provider(sampler=ALWAYS_OFF)
        monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    reads: list[str] = []

    def policy_read() -> Any:
        reads.append("read")
        raise AssertionError("an untraced tool call must not read the policy")

    monkeypatch.setattr(content_module, "active_content_policy", policy_read)
    sentinel = object()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        returned = _span_middleware().wrap_tool_call(_request(), lambda request: sentinel)
    assert returned is sentinel
    assert reads == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_tool_span_telemetry_failure_never_breaks_the_call_and_warns_once(
    recording, monkeypatch, caplog
):
    """A failing content write never fails the tool call: the call returns
    its own result, the outcome is still recorded, and the failure is one
    WARNING per process."""
    from inqtrix.agents.kernel import middleware as middleware_module

    _capture(monkeypatch, on=True)
    monkeypatch.setattr(middleware_module, "_tool_span_warned", set())

    def broken(*args: Any, **kwargs: Any) -> Any:
        # Not a ValueError: the guard holds for any telemetry exception.
        raise RuntimeError("content path broken")

    monkeypatch.setattr(content_module, "record_content", broken)
    result = ToolMessage("ok", tool_call_id="c1")
    middleware = _span_middleware()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        for _ in range(2):
            assert middleware.wrap_tool_call(_request(), lambda request: result) is result

    spans = _tool_spans(recording)
    assert [s.attributes["inqtrix.outcome"] for s in spans] == ["completed", "completed"]
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.getMessage().startswith("Tool span telemetry failed")
    ]
    assert len(warnings) == 1


# -- timeline rows and child runs over the platform path -------------------- #


def _desk_turn(*calls: ToolCallRequest) -> ChatTurn:
    return ChatTurn(
        text="",
        tool_calls=tuple(calls),
        finish_reason="tool_calls",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _desk_text(text: str) -> ChatTurn:
    return ChatTurn(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        model="high-model",
        prompt_tokens=10,
        completion_tokens=5,
        raw=None,
    )


def _finished(row: dict[str, Any], facts: SimpleNamespace) -> dict[str, Any]:
    return facts.finished.get(row["invocation_id"]) or {}


def _rejected_at_the_gate(row: dict[str, Any], facts: SimpleNamespace) -> bool:
    return _finished(row, facts).get("status") == "error" and any(
        approval["kind"] == "tool"
        and approval["decision"] == "reject"
        and any(
            action.get("tool") == row["tool"]
            for action in (approval.get("payload") or {}).get("actions", [])
        )
        for approval in facts.approvals
    )


def _parallel_write_todos(row: dict[str, Any], facts: SimpleNamespace) -> bool:
    return (
        row["tool"] == "write_todos"
        and _finished(row, facts).get("status") == "error"
        and sum(1 for other in facts.started if other["tool"] == "write_todos") > 1
    )


INVENTED_TOOL_NAME = "MARKER_INVENTED_tool"
"""A tool name no kernel tool has, as a model may write one."""


UNEXECUTED_TOOL_ROWS: dict[str, Callable[[dict[str, Any], SimpleNamespace], bool]] = {
    "hitl_reject": _rejected_at_the_gate,
    "parallel_write_todos": _parallel_write_todos,
}
"""Timeline rows of this test's runs that never pass the tool node and
therefore have no tool span, each with the check that ties a row to that
cause. A row without a span that no check explains is a missing observation.
The full list of such causes is in the tracing legend; only causes these runs
produce are checked here."""


def _run_facts(client: Any, run_id: str) -> SimpleNamespace:
    events = client.get(f"/v1/runs/{run_id}/events?format=json").json()["data"]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        by_type.setdefault(event["type"], []).append(event["data"])
    summary = client.get(f"/v1/runs/{run_id}").json()
    return SimpleNamespace(
        run_id=run_id,
        events=by_type,
        started=by_type.get("inqtrix.agent.tool.started", []),
        finished={
            row["invocation_id"]: row
            for row in by_type.get("inqtrix.agent.tool.finished", [])
        },
        approvals=client.get(f"/v1/runs/{run_id}/approvals").json()["data"],
        children=client.get(f"/v1/runs/{run_id}/children").json()["data"],
        work_profile=str(summary.get("work_profile") or ""),
    )


def test_tool_spans_join_timeline_rows_with_explicit_exceptions(
    recording, monkeypatch
):
    """Every tool row of a run has its tool observation (same call id and
    tool, exactly one terminal span, under that run), unless a named cause
    explains why the call never reached the tool node; every tool span has
    its row."""
    from tests.agents.test_kernel_prompt_events import (
        ScriptedToolLLM,
        make_client,
        wait_status,
    )

    _capture(monkeypatch, on=False)
    autonomous = ScriptedToolLLM(
        [
            _desk_turn(
                ToolCallRequest(id="c_web", name="web_instant", arguments={"query": "Eventtest"}),
                ToolCallRequest(
                    id="c_todo",
                    name="write_todos",
                    arguments={
                        "todos": [{"content": "Quelle prüfen", "status": "in_progress"}]
                    },
                ),
            ),
            _desk_turn(
                ToolCallRequest(id="c_excl", name="read_file", arguments={"file_path": "/x"}),
                ToolCallRequest(id="c_fake", name=INVENTED_TOOL_NAME, arguments={}),
            ),
            _desk_turn(
                ToolCallRequest(
                    id="c_todo_a",
                    name="write_todos",
                    arguments={"todos": [{"content": "A", "status": "pending"}]},
                ),
                ToolCallRequest(
                    id="c_todo_b",
                    name="write_todos",
                    arguments={"todos": [{"content": "B", "status": "pending"}]},
                ),
            ),
            _desk_text("Fertig."),
        ]
    )
    strict = ScriptedToolLLM(
        [
            _desk_turn(
                ToolCallRequest(id="c_rej", name="web_instant", arguments={"query": "extern"})
            ),
            _desk_text("Ohne Websuche beantwortet."),
        ]
    )
    runs: list[SimpleNamespace] = []
    for llm, autonomy in ((autonomous, "autonomous"), (strict, "strict")):
        client = make_client(llm)
        with client:
            response = client.post(
                "/v1/runs",
                json={
                    "question": "Suche bitte die aktuelle Quelle.",
                    "mode": "agent_kernel",
                    "autonomy": autonomy,
                },
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            if autonomy == "strict":
                wait_status(client, run_id, {"waiting_for_approval"})
                (approval,) = [
                    row
                    for row in client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
                    if row["status"] == "pending"
                ]
                decided = client.post(
                    f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
                    json={"decision": "reject", "note": "Keine externe Suche."},
                )
                assert decided.status_code == 200, decided.text
            wait_status(client, run_id, {"completed"})
            runs.append(_run_facts(client, run_id))

    expected_runs = {facts.run_id for facts in runs}

    def runs_closed() -> list[Any]:
        spans = recording.get_finished_spans()
        closed = {
            span.attributes.get("inqtrix.run_id")
            for span in spans
            if span.name == "inqtrix.run"
        }
        return list(spans) if expected_runs <= closed else []

    spans = _wait_for(runs_closed)
    assert spans, "the run spans never closed"
    by_id = {span.context.span_id: span for span in spans}
    tool_spans = [
        span
        for span in spans
        if (span.attributes or {}).get("gen_ai.operation.name") == "execute_tool"
    ]
    explained: dict[str, list[str]] = {}
    joined: set[str] = set()
    for facts in runs:
        for row in facts.started:
            matching = [
                span
                for span in tool_spans
                if span.attributes.get("gen_ai.tool.call.id") == row["invocation_id"]
            ]
            if not matching:
                causes = [
                    cause
                    for cause, check in UNEXECUTED_TOOL_ROWS.items()
                    if check(row, facts)
                ]
                assert causes, f"tool row without a span and without a cause: {row}"
                explained[row["invocation_id"]] = causes
                continue
            joined.add(row["invocation_id"])
            # A name the model invented joins by call id only: the span says
            # unknown_tool and carries the invented name nowhere (capture off).
            invented = row["tool"] == INVENTED_TOOL_NAME
            assert {span.attributes["gen_ai.tool.name"] for span in matching} == (
                {"unknown_tool"} if invented else {row["tool"]}
            )
            if invented:
                for span in matching:
                    assert INVENTED_TOOL_NAME not in everything_a_span_exports(span)
            terminal = [
                span
                for span in matching
                if span.attributes.get("inqtrix.outcome") != "parked"
            ]
            assert len(terminal) == 1, row
            for span in matching:
                run_span = _ancestor_run(span, by_id)
                assert run_span is not None, row
                assert run_span.attributes["inqtrix.run_id"] == facts.run_id
    assert joined == {"c_web", "c_todo", "c_excl", "c_fake"}
    assert explained == {
        "c_rej": ["hitl_reject"],
        "c_todo_a": ["parallel_write_todos"],
        "c_todo_b": ["parallel_write_todos"],
    }
    started_ids = {row["invocation_id"] for facts in runs for row in facts.started}
    assert {
        span.attributes.get("gen_ai.tool.call.id") for span in tool_spans
    } <= started_ids


def test_child_run_parents_to_the_parked_tool_span(recording, monkeypatch):
    """A child submitted from a tool body carries the context of the tool
    span that submitted it, which parks; the resumed tool execution is a
    second span with the same call id under the next segment's run span and
    finds the child instead of submitting another."""
    from tests.agents.test_kernel_child_runs import (
        ScriptedToolLLM,
        _text_turn as _child_text_turn,
        _tool_turn as _child_tool_turn,
        make_client,
        wait_status,
    )

    _capture(monkeypatch, on=False)
    llm = ScriptedToolLLM(
        [
            _child_tool_turn(
                "call_research1",
                "run_web_research",
                {"question": "Marktlage Klimaanlagen 2026"},
            ),
            _child_text_turn("Antwort auf Basis des Kindberichts."),
        ]
    )
    client = make_client(monkeypatch, llm)
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Analysiere die Marktlage.",
                "mode": "agent_kernel",
                "autonomy": "autonomous",
                "tool_directives": ["web_research"],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        wait_status(client, run_id, {"completed"})
        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        child_id = children[0]["run_id"]

        def research_spans() -> list[Any]:
            spans = recording.get_finished_spans()
            by_id = {span.context.span_id: span for span in spans}
            research = sorted(
                (
                    span
                    for span in spans
                    if span.name == "execute_tool run_web_research"
                    and span.attributes.get("gen_ai.tool.call.id") == "call_research1"
                ),
                key=lambda span: span.end_time,
            )
            child_runs = [
                span
                for span in spans
                if span.name == "inqtrix.run"
                and span.attributes.get("inqtrix.run_id") == child_id
            ]
            ready = (
                len(research) == 2
                and child_runs
                and all(_ancestor_run(span, by_id) is not None for span in research)
            )
            return [research, child_runs, by_id] if ready else []

        found = _wait_for(research_spans)

    assert found, "the parked and resumed tool spans or the child run span never closed"
    research, child_runs, by_id = found
    assert [span.attributes["inqtrix.outcome"] for span in research] == [
        "parked",
        "completed",
    ]
    parked, completed = research
    for child_run in child_runs:
        assert child_run.parent is not None
        assert child_run.parent.span_id == parked.context.span_id
    first_segment = _ancestor_run(parked, by_id)
    second_segment = _ancestor_run(completed, by_id)
    assert first_segment.attributes["inqtrix.run_id"] == run_id
    assert second_segment.attributes["inqtrix.run_id"] == run_id
    assert first_segment.context.span_id != second_segment.context.span_id
