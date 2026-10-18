"""Real SDK compaction calls preserve the kernel's run-local contracts."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

pytest.importorskip("deepagents")

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

from inqtrix.agents.harness import _kernel_summarization_middleware
from inqtrix.agents.kernel.chat_bridge import build_tool_chat_model
from inqtrix.agents.kernel.deps import KernelDeps, kernel_deps, set_kernel_deps
from inqtrix.agents.kernel.middleware import (
    CONTEXT_COMPACTED_EVENT,
    SUMMARY_CHARS_KEY,
    current_compaction_assignment,
    current_compaction_todos,
)
from inqtrix.exceptions import AgentCancelled
from inqtrix.settings import AgentPlatformSettings
from tests.agents.test_harness_kernel_contract import _ScriptedProvider, _text_turn


def _deps(run_id, provider, archives, events):
    deps = KernelDeps(
        run_id=run_id,
        control=None,
        platform=AgentPlatformSettings(),
        llm=provider,
        model=None,
        reasoning_effort=None,
        timeout=2.0,
        context_trigger_tokens=1,
        event_sink=lambda kind, payload: events.append((kind, payload)),
    )

    def archive(title, text):
        archives.append((title, text))
        return f"{run_id}_source"

    deps.append_context_archive = archive
    return deps


def _request(model, marker):
    messages = [
        HumanMessage(content=f"{marker}START " + "source content " * 4_000),
        AIMessage(
            content=f"{marker}END",
            usage_metadata={"input_tokens": 17, "output_tokens": 5, "total_tokens": 22},
        ),
        HumanMessage(content="Continue."),
    ]
    return ModelRequest(
        model=model,
        messages=messages,
        state={
            "messages": messages,
            "todos": [{"content": f"{marker}TASK", "status": "in_progress"}],
        },
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_compaction_archives_full_history_and_preserves_usage(asynchronous):
    provider = _ScriptedProvider([_text_turn("Summary."), _text_turn("Answer.")])
    usage = []
    model = build_tool_chat_model(provider, usage_hook=lambda *tokens: usage.append(tokens))
    middleware = _kernel_summarization_middleware(model, keep_messages=1)
    archives, events, projections = [], [], []
    deps = _deps("runA", provider, archives, events)
    request = _request(model, "MARKE1")
    original = deepcopy(request.state)

    def handler(projected):
        projections.append(projected)
        return ModelResponse(result=[model.invoke(projected.messages)])

    async def async_handler(projected):
        projections.append(projected)
        return ModelResponse(result=[await model.ainvoke(projected.messages)])

    set_kernel_deps(deps)
    try:
        if asynchronous:
            response = asyncio.run(middleware.awrap_model_call(request, async_handler))
        else:
            response = middleware.wrap_model_call(request, handler)
    finally:
        set_kernel_deps(None)

    assert request.state == original
    assert len(archives) == 1
    assert archives[0][1] == (
        "[human] " + request.messages[0].content + "\n\n[ai] MARKE1END"
    )
    # Check the model's actual summary input, not just its configured cap.
    summary_input = provider.calls[0]["messages"][0]["content"]
    assert "MARKE1START" in summary_input and "MARKE1END" in summary_input
    # The condensed conversation reaches the summary model as data, fenced.
    fenced = summary_input.split('<untrusted_content source="older-conversation">', 1)[1]
    assert "MARKE1START" in fenced.split("</untrusted_content>", 1)[0]
    assert summary_input.count("source content") == 4_000
    update = response.command.update
    assert update["_summarization_session_id"]
    event = update["_summarization_event"]
    assert event["cutoff_index"] == 2
    assert event["file_path"] == "runA_source"
    projected_summary = projections[0].messages[0]
    assert "read_canvas(artifact_id='runA_source')" in projected_summary.content
    assert "MARKE1TASK" in projected_summary.content
    assert "read_file" not in projected_summary.content
    # Marked as a summary, so a later compaction does not re-archive it, and
    # carrying the length of the summary itself, not of the message.
    assert projected_summary.additional_kwargs["lc_source"] == "summarization"
    assert projected_summary.additional_kwargs[SUMMARY_CHARS_KEY] == len("Summary.")
    assert len(projections) == 1
    assert usage == [(1, 1), (1, 1)]
    assert len([kind for kind, _ in events if kind == CONTEXT_COMPACTED_EVENT]) == 1
    assert current_compaction_todos.get() is None
    assert current_compaction_assignment.get() == ""


@pytest.mark.parametrize("asynchronous", [False, True])
def test_summary_cancellation_has_no_outer_retry(asynchronous):
    class CancelledProvider(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            self.calls.append({"messages": messages})
            raise AgentCancelled("Cancelled by the caller.")

    provider = CancelledProvider([])
    model = build_tool_chat_model(provider)
    middleware = _kernel_summarization_middleware(model, keep_messages=1)
    archives, events = [], []
    set_kernel_deps(_deps("runCancelled", provider, archives, events))

    def handler(_request):
        pytest.fail("The answer must not run after summary cancellation.")

    async def async_handler(request):
        return handler(request)

    try:
        with pytest.raises(AgentCancelled, match="Cancelled by the caller"):
            request = _request(model, "MARKE2")
            if asynchronous:
                asyncio.run(middleware.awrap_model_call(request, async_handler))
            else:
                middleware.wrap_model_call(request, handler)
    finally:
        set_kernel_deps(None)
    assert len(provider.calls) == 1
    assert len(archives) == 1
    assert events == []
    assert current_compaction_todos.get() is None


def test_shared_middleware_keeps_parallel_run_contexts_isolated():
    rendezvous = Barrier(2, timeout=5)

    class ContextProvider(_ScriptedProvider):
        def chat(self, messages, **kwargs):
            run_id = kernel_deps().run_id
            rendezvous.wait()
            return _text_turn(f"Summary for {run_id}.")

    provider = ContextProvider([])
    model = build_tool_chat_model(provider)
    middleware = _kernel_summarization_middleware(model, keep_messages=1)

    def compact(run_id):
        archives, events = [], []
        set_kernel_deps(_deps(run_id, provider, archives, events))
        try:
            response = middleware.wrap_model_call(
                _request(model, run_id),
                lambda request: ModelResponse(result=[AIMessage(content="Done.")]),
            )
            assert current_compaction_todos.get() is None
            return response.command.update["_summarization_event"], archives, events
        finally:
            set_kernel_deps(None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compact, ["MARKE3", "MARKE4"]))
    for own, other, (event, archives, events) in zip(
        ["MARKE3", "MARKE4"], ["MARKE4", "MARKE3"], results, strict=True
    ):
        summary = event["summary_message"].content
        assert f"Summary for {own}." in summary
        assert f"{own}TASK" in summary
        assert other not in summary
        assert own in archives[0][1] and other not in archives[0][1]
        payload = next(p for k, p in events if k == CONTEXT_COMPACTED_EVENT)
        assert payload["archive_artifact_id"] == f"{own}_source"
