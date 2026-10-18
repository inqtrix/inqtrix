"""Tests for the implicit-cancel-on-disconnect pathway."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.core.results import RunRequest
from inqtrix.exceptions import AgentCancelled
from inqtrix.providers.base import ProviderContext
from inqtrix.research.web_research import WebResearchAlgorithm
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.server.streaming import stream_response
from inqtrix.settings import AgentSettings, Settings
from inqtrix.state import check_cancel_event, initial_state
from inqtrix.server.execution import ExecutionLanes

# Shared per module: these tests' fake agents return promptly, so a narrow
# lane never becomes the bottleneck under test. Released at module teardown.
_LANES = ExecutionLanes(ai_workers=8, stream_workers=8)


def teardown_module(module) -> None:  # noqa: ARG001 - pytest hook signature
    _LANES.close()


# ------------------------------------------------------------------ #
# state.check_cancel_event — unit
# ------------------------------------------------------------------ #


def test_check_cancel_event_noop_when_field_missing():
    state = {"question": "x"}
    check_cancel_event(state)  # must not raise


def test_check_cancel_event_noop_when_event_unset():
    event = threading.Event()
    state = {"question": "x", "_cancel_event": event}
    check_cancel_event(state)  # must not raise


@pytest.mark.parametrize("engine", ["research", "kernel"])
def test_cancel_event_does_not_infer_a_disconnect(engine):
    event = threading.Event()
    event.set()
    if engine == "research":
        def check():
            check_cancel_event({"question": "x", "_cancel_event": event})
    else:
        from inqtrix.agents.kernel.deps import KernelDeps
        from inqtrix.settings import AgentPlatformSettings

        deps = KernelDeps(
            run_id="run_cancel_wording",
            control=None,
            platform=AgentPlatformSettings(),
            llm=None,
            model=None,
            reasoning_effort=None,
            timeout=1.0,
            cancel_token=event,
        )
        check = deps.check_abort
    with pytest.raises(AgentCancelled) as cancelled:
        check()
    # The event carries no cause: UI stop and transport loss share it.
    assert str(cancelled.value) == "Run cancelled."


def test_initial_state_accepts_optional_cancel_event():
    event = threading.Event()
    state = initial_state("question", cancel_event=event)
    assert state["_cancel_event"] is event


def test_initial_state_omits_cancel_event_when_none():
    state = initial_state("question")
    assert "_cancel_event" not in state


# ------------------------------------------------------------------ #
# graph.run — cancel handling
# ------------------------------------------------------------------ #


def test_graph_run_returns_cancelled_state_when_cancelled(monkeypatch):
    """Mock the LangGraph compiled agent so .invoke raises AgentCancelled."""
    from inqtrix import graph as graph_module

    class _StubAgent:
        def invoke(self, state):
            raise AgentCancelled("simulated client disconnect")

    monkeypatch.setattr(graph_module, "get_agent", lambda *a, **kw: _StubAgent())

    providers = SimpleNamespace(llm=SimpleNamespace(), search=SimpleNamespace())
    strategies = SimpleNamespace()
    settings = AgentSettings()
    cancel_event = threading.Event()
    cancel_event.set()

    result = graph_module.run(
        "question",
        providers=providers,
        strategies=strategies,
        settings=settings,
        cancel_event=cancel_event,
    )
    assert result["answer"] == ""
    assert result["result_state"]["cancelled"] is True


@pytest.mark.parametrize("budget_cancel", [False, True])
def test_graph_run_keeps_current_node_usage_when_interrupted(monkeypatch, budget_cancel):
    """The real graph copies scalar state; an invoke stub hides that boundary."""
    from inqtrix import graph as graph_module
    from inqtrix.exceptions import AgentTokenBudgetExceeded

    def classify(state, **_kwargs):
        state["total_prompt_tokens"] += 13
        state["total_completion_tokens"] += 2
        state["done"] = True
        return state

    def answer(state, **_kwargs):
        state["total_prompt_tokens"] += 7
        state["total_completion_tokens"] += 3
        state["round"] = 1
        exception = AgentTokenBudgetExceeded if budget_cancel else AgentCancelled
        raise exception("Stop after consumed provider responses")

    monkeypatch.setattr("inqtrix.nodes.classify", classify)
    monkeypatch.setattr("inqtrix.nodes.answer", answer)
    result = graph_module.run(
        "question",
        providers=SimpleNamespace(llm=SimpleNamespace(), search=SimpleNamespace()),
        strategies=SimpleNamespace(),
        settings=AgentSettings(),
    )

    assert result["answer"] == ""
    assert result["usage"] == {"prompt_tokens": 20, "completion_tokens": 5}
    assert result["result_state"]["round"] == 1
    assert result["result_state"]["cancelled"] is True
    assert result["result_state"]["cancel_reason"] == (
        "token_budget_exceeded" if budget_cancel else "client_requested_cancel"
    )


def test_graph_run_keeps_usage_separate_for_concurrent_interruptions(monkeypatch):
    """Two invocations share a cached graph, but never an interrupted state."""
    from concurrent.futures import ThreadPoolExecutor

    from inqtrix import graph as graph_module

    barrier = threading.Barrier(2, timeout=5)

    def classify(state, **_kwargs):
        state["done"] = True
        return state

    def answer(state, **_kwargs):
        if state["question"] == "first":
            state["total_prompt_tokens"] = 17
            state["total_completion_tokens"] = 3
        else:
            state["total_prompt_tokens"] = 41
            state["total_completion_tokens"] = 7
        barrier.wait()
        raise AgentCancelled("Stop this invocation")

    monkeypatch.setattr("inqtrix.nodes.classify", classify)
    monkeypatch.setattr("inqtrix.nodes.answer", answer)
    providers = SimpleNamespace(llm=SimpleNamespace(), search=SimpleNamespace())
    strategies = SimpleNamespace()
    settings = AgentSettings()
    # Warm the shared graph so the two invocations certainly use one instance.
    graph_module.get_agent(providers, strategies, settings)

    def run(question):
        return graph_module.run(
            question, providers=providers, strategies=strategies, settings=settings
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(run, ["first", "second"]))

    assert first["usage"] == {"prompt_tokens": 17, "completion_tokens": 3}
    assert second["usage"] == {"prompt_tokens": 41, "completion_tokens": 7}
    assert first["result_state"]["question"] == "first"
    assert second["result_state"]["question"] == "second"


def test_graph_run_keeps_completed_usage_unchanged(monkeypatch):
    from inqtrix import graph as graph_module

    def classify(state, **_kwargs):
        state["total_prompt_tokens"] += 13
        state["total_completion_tokens"] += 2
        state["done"] = True
        return state

    def answer(state, **_kwargs):
        state["total_prompt_tokens"] += 7
        state["total_completion_tokens"] += 3
        state["answer"] = "Complete answer"
        return state

    monkeypatch.setattr("inqtrix.nodes.classify", classify)
    monkeypatch.setattr("inqtrix.nodes.answer", answer)
    result = graph_module.run(
        "question",
        providers=SimpleNamespace(llm=SimpleNamespace(), search=SimpleNamespace()),
        strategies=SimpleNamespace(),
        settings=AgentSettings(),
    )

    assert result["answer"] == "Complete answer"
    assert result["usage"] == {"prompt_tokens": 20, "completion_tokens": 5}
    assert not result["result_state"].get("cancelled")


# ------------------------------------------------------------------ #
# stream_response — disconnect handling
# ------------------------------------------------------------------ #


class _DummyLLM:
    def complete(self, *a, **kw): return "ok"
    def is_available(self): return True


class _DummySearch:
    def search(self, *a, **kw):
        return GroundedSearchResult()
    def is_available(self): return True


@pytest.mark.asyncio
async def test_stream_response_passes_cancel_event_to_run_web_graph(monkeypatch):
    """Verify the cancel_event arrives at the graph seam as a kwarg.

    Streaming dispatches through ``WebResearchAlgorithm.run`` -> ``_execute_graph``
    -> ``run_web_graph``; the RunContext's ``cancel_token`` becomes the graph's
    ``cancel_event`` kwarg, so the disconnect probe still reaches node boundaries.
    """
    captured: dict[str, Any] = {}

    def fake_run_web_graph(question, **kwargs):
        captured["cancel_event"] = kwargs.get("cancel_event")
        return {"answer": "Hallo Welt", "result_state": {}}

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run_web_graph)

    event = threading.Event()
    chunks = [
        chunk
        async for chunk in stream_response(
            "Frage",
            algorithm=WebResearchAlgorithm(),
            runtime=None,
            run_request=RunRequest(mode="research", question="Frage", history=""),
            providers=None, strategies=None, settings=AgentSettings(),
            lanes=_LANES,
            cancel_event=event,
        )
    ]
    assert captured["cancel_event"] is event
    assert any("Hallo " in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_stream_response_sets_cancel_event_on_disconnect(monkeypatch):
    """Simulate a Request whose receive() yields http.disconnect; cancel_event must be set.

    Replaces the previous polling-based ``is_disconnected`` test: the
    cancellation pathway spawns a watcher task that blocks on
    ``await request.receive()`` and acts on the first
    ``http.disconnect`` message uvicorn emits. The fake request below
    delays the disconnect by a tiny amount so the streaming loop has a
    chance to enter the progress-read path before the cancel fires.
    """

    def slow_run(question, **kwargs):
        # Simulate a long-running agent: keep emitting progress until cancelled.
        progress_queue = kwargs.get("progress_queue")
        cancel_event = kwargs.get("cancel_event")
        if progress_queue is not None:
            for i in range(200):
                if cancel_event is not None and cancel_event.is_set():
                    break
                progress_queue.put(("progress", f"step {i}"))
                time.sleep(0.05)
        return {"answer": "never delivered", "result_state": {}}

    monkeypatch.setattr(web_research_module, "run_web_graph", slow_run)

    class _FakeRequest:
        def __init__(self):
            self._delivered = False

        async def receive(self) -> dict:
            # First call: tiny await so the streaming loop can yield the
            # role chunk and start polling. Then deliver http.disconnect.
            if not self._delivered:
                self._delivered = True
                await asyncio.sleep(0.2)
                return {"type": "http.disconnect"}
            # Subsequent receive() calls would normally block forever;
            # the watcher task should have exited after the first
            # disconnect, so this branch is unreachable in practice.
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

        async def is_disconnected(self) -> bool:
            # Kept for backwards-compat with any caller that might still
            # probe it; the new watcher path does not call this.
            return self._delivered

    event = threading.Event()
    request = _FakeRequest()

    chunks: list[str] = []
    async for chunk in stream_response(
        "Frage",
        algorithm=WebResearchAlgorithm(),
        runtime=None,
        run_request=RunRequest(mode="research", question="Frage", history=""),
        providers=None, strategies=None, settings=AgentSettings(),
        lanes=_LANES,
        request=request,
        cancel_event=event,
    ):
        chunks.append(chunk)

    assert event.is_set(), "Cancel event must be set after detected disconnect"
    # The generator returns early on disconnect; no [DONE] tail expected.
    assert not any("data: [DONE]" in chunk for chunk in chunks)


@pytest.mark.asyncio
async def test_watch_disconnect_signals_cancel_on_http_disconnect():
    """Direct unit test for the watcher helper: http.disconnect -> cancel_event.set()."""
    from inqtrix.server.streaming import watch_disconnect

    class _ImmediateDisconnectRequest:
        async def receive(self) -> dict:
            return {"type": "http.disconnect"}

    event = threading.Event()
    await watch_disconnect(_ImmediateDisconnectRequest(), event)
    assert event.is_set(), "Watcher must set cancel_event on http.disconnect"


@pytest.mark.asyncio
async def test_watch_disconnect_treats_receive_error_as_disconnect():
    """If receive() raises (e.g. transport torn down), watcher must still cancel."""
    from inqtrix.server.streaming import watch_disconnect

    class _BrokenRequest:
        async def receive(self) -> dict:
            raise RuntimeError("transport gone")

    event = threading.Event()
    await watch_disconnect(_BrokenRequest(), event)
    assert event.is_set(), (
        "Watcher must set cancel_event when receive() crashes — treating an "
        "unreadable transport as a disconnect avoids burning tokens for a run "
        "whose response can no longer be delivered."
    )


@pytest.mark.asyncio
async def test_watch_disconnect_cancellation_propagates():
    """The watcher must propagate asyncio.CancelledError so cleanup awaits resolve."""
    from inqtrix.server.streaming import watch_disconnect

    class _SilentRequest:
        async def receive(self) -> dict:
            await asyncio.sleep(60)  # block forever
            return {"type": "http.disconnect"}

    event = threading.Event()
    task = asyncio.create_task(watch_disconnect(_SilentRequest(), event))
    await asyncio.sleep(0.05)  # let the task enter receive()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not event.is_set(), (
        "Watcher must NOT set cancel_event when its task is externally "
        "cancelled (normal stream completion path) — only when the client "
        "actually disconnects."
    )


@pytest.mark.asyncio
async def test_stream_response_cleans_up_watcher_on_normal_completion(monkeypatch):
    """Normal stream completion must cancel the watcher task without leaking."""
    monkeypatch.setattr(
        web_research_module,
        "run_web_graph",
        lambda question, **kwargs: {"answer": "ok", "result_state": {}},
    )

    class _NeverDisconnectRequest:
        async def receive(self) -> dict:
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

    request = _NeverDisconnectRequest()
    chunks: list[str] = []
    async for chunk in stream_response(
        "Frage",
        algorithm=WebResearchAlgorithm(),
        runtime=None,
        run_request=RunRequest(mode="research", question="Frage", history=""),
        providers=None, strategies=None, settings=AgentSettings(),
        lanes=_LANES,
        request=request,
    ):
        chunks.append(chunk)

    # Stream completed normally with [DONE] sentinel; no leftover tasks.
    assert any("data: [DONE]" in chunk for chunk in chunks)
    pending = [
        t for t in asyncio.all_tasks() if "watch_disconnect" in t.get_coro().__qualname__
    ]
    assert pending == [], f"Watcher task leaked after normal completion: {pending}"


# ------------------------------------------------------------------ #
# Blocking path — carries the disconnect cancel event
# ------------------------------------------------------------------ #


def test_chat_completions_blocking_path_carries_a_cancel_event(monkeypatch):
    """Blocking /v1/chat/completions wires the disconnect cancel event.

    The non-streaming transport shares the SSE disconnect semantics: the
    route's watcher event reaches the graph as ``cancel_event``, so a
    client abort stops the run at its next checkpoint.
    """
    def fake_run(
        question,
        *,
        history,
        providers,
        strategies,
        settings,
        cancel_event,
    ):
        assert isinstance(cancel_event, threading.Event)
        assert not cancel_event.is_set()
        return {
            "answer": "ok",
            "result_state": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    providers = ProviderContext(llm=_DummyLLM(), search=_DummySearch())
    app = create_app(settings=Settings(), providers=providers)
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
        )
    assert response.status_code == 200


# ------------------------------------------------------------------ #
# disconnect_watch context manager (shared by chat JSON + editor routes)
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_disconnect_watch_sets_event_and_never_leaks():
    """Disconnect inside the block flips the event; exit reaps the task."""
    from inqtrix.server.streaming import disconnect_watch

    class _ImmediateDisconnectRequest:
        async def receive(self) -> dict:
            return {"type": "http.disconnect"}

    async with disconnect_watch(_ImmediateDisconnectRequest()) as cancel_event:
        deadline = asyncio.get_event_loop().time() + 2.0
        while not cancel_event.is_set():
            if asyncio.get_event_loop().time() > deadline:
                pytest.fail("disconnect never flipped the cancel event")
            await asyncio.sleep(0.01)
    pending = [
        t for t in asyncio.all_tasks()
        if "watch_disconnect" in t.get_coro().__qualname__
    ]
    assert pending == []


@pytest.mark.asyncio
async def test_disconnect_watch_exit_does_not_set_event():
    """Normal completion reaps the watcher WITHOUT signalling a cancel."""
    from inqtrix.server.streaming import disconnect_watch

    class _SilentRequest:
        async def receive(self) -> dict:
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

    async with disconnect_watch(_SilentRequest()) as cancel_event:
        pass
    assert not cancel_event.is_set()
    pending = [
        t for t in asyncio.all_tasks()
        if "watch_disconnect" in t.get_coro().__qualname__
    ]
    assert pending == []


# ------------------------------------------------------------------ #
# Non-streaming chat: client abort answers 499 instead of burning budget
# ------------------------------------------------------------------ #


def _chat_service_for(algorithm):
    from types import SimpleNamespace

    from inqtrix.services.chat_service import ChatService

    registry = SimpleNamespace(get=lambda _mode: algorithm)
    return ChatService(registry=registry, runtime=None)


def _resolved_stub():
    from types import SimpleNamespace

    return SimpleNamespace(
        mode="knowledge",
        agent_overrides={},
        knowledge_filters={},
        providers=None,
        strategies=None,
    )


@pytest.mark.asyncio
async def test_non_streaming_complete_maps_cancel_to_499():
    class _CancelledAlgorithm:
        def run(self, _request, *, runtime, context):
            del runtime
            assert context.cancel_token is not None, (
                "the route's cancel event must reach the algorithm"
            )
            raise AgentCancelled("stop")

    service = _chat_service_for(_CancelledAlgorithm())
    cancel_event = threading.Event()
    cancel_event.set()
    response = await service.complete(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved_stub(),
        chat_agent_settings=AgentSettings(),
        lanes=_LANES,
        semaphore=asyncio.Semaphore(1),
        cancel_event=cancel_event,
    )
    assert response.status_code == 499
    import json as _json

    body = _json.loads(response.body)
    assert body["error"]["type"] == "client_closed_request"


@pytest.mark.asyncio
async def test_non_streaming_cancel_without_disconnect_is_a_server_error():
    """An AgentCancelled with NO disconnect must not claim the client left."""

    class _CancelledAlgorithm:
        def run(self, _request, *, runtime, context):
            del runtime, context
            raise AgentCancelled("stop")

    service = _chat_service_for(_CancelledAlgorithm())
    response = await service.complete(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved_stub(),
        chat_agent_settings=AgentSettings(),
        lanes=_LANES,
        semaphore=asyncio.Semaphore(1),
        cancel_event=threading.Event(),
    )
    assert response.status_code == 502
    import json as _json

    body = _json.loads(response.body)
    assert body["error"]["type"] == "server_error"


@pytest.mark.asyncio
async def test_non_streaming_token_budget_stop_is_typed_with_usage():
    """The AgentCancelled SUBCLASS keeps its resource meaning and usage."""
    from inqtrix.exceptions import AgentTokenBudgetExceeded

    class _BudgetStopAlgorithm:
        def run(self, _request, *, runtime, context):
            del runtime, context
            raise AgentTokenBudgetExceeded(
                "Tokenbudget erreicht.",
                usage={"prompt_tokens": 11, "completion_tokens": 4},
            )

    service = _chat_service_for(_BudgetStopAlgorithm())
    response = await service.complete(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved_stub(),
        chat_agent_settings=AgentSettings(),
        lanes=_LANES,
        semaphore=asyncio.Semaphore(1),
        cancel_event=threading.Event(),
    )
    assert response.status_code == 502
    import json as _json

    body = _json.loads(response.body)
    assert body["error"]["type"] == "token_budget_exceeded"
    assert response.inqtrix_usage == {
        "prompt_tokens": 11,
        "completion_tokens": 4,
    }


@pytest.mark.asyncio
async def test_non_streaming_cancelled_graph_result_maps_to_499_with_usage():
    from inqtrix.core.results import AgentResult

    class _GraphCancelledAlgorithm:
        def run(self, _request, *, runtime, context):
            del runtime, context
            return AgentResult(
                answer="",
                raw={
                    "answer": "",
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    "result_state": {"cancelled": True},
                },
            )

    service = _chat_service_for(_GraphCancelledAlgorithm())
    response = await service.complete(
        question="Frage",
        history="",
        messages=[],
        resolved=_resolved_stub(),
        chat_agent_settings=AgentSettings(),
        lanes=_LANES,
        semaphore=asyncio.Semaphore(1),
        cancel_event=threading.Event(),
    )
    assert response.status_code == 499
    assert response.inqtrix_usage == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
    }


@pytest.mark.asyncio
async def test_non_stream_route_cancels_on_client_disconnect(monkeypatch):
    """ASGI-level: a disconnect after the JSON body cancels the blocking run.

    Drives the real app with a scripted receive channel: one
    ``http.request`` carrying the body, then ``http.disconnect``. The
    watcher must park on the post-body ``receive()``, flip the event,
    and the route must answer 499 after the graph observed the cancel.
    """
    import json as _json

    observed: dict[str, Any] = {}

    def slow_run(
        question, *, history, providers, strategies, settings, cancel_event
    ):
        for _ in range(200):
            if cancel_event.is_set():
                observed["cancelled"] = True
                raise AgentCancelled("client disconnect observed")
            time.sleep(0.05)
        return {"answer": "never", "result_state": {}, "usage": {}}

    monkeypatch.setattr(web_research_module, "run_web_graph", slow_run)

    providers = ProviderContext(llm=_DummyLLM(), search=_DummySearch())
    # Works without running the lifespan because the non-postgres default
    # marks the database contract ready at create_app time; a
    # postgres-backed Settings fixture would need the lifespan (503 gate).
    app = create_app(settings=Settings(), providers=providers)

    body = _json.dumps(
        {"messages": [{"role": "user", "content": "Hallo"}], "stream": False}
    ).encode()
    inbox = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict:
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(0.3)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"host", b"testserver"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    assert observed.get("cancelled") is True, (
        "the graph must observe the disconnect-driven cancel event"
    )
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 499


def _suggest_request():
    from inqtrix.server.editor_suggestions import parse_editor_suggest_payload

    return parse_editor_suggest_payload(
        {"block_text": "Ein Absatz.", "instruction": "Formeller machen."},
        max_text_chars=10_000,
        max_background_chars=10_000,
    )


class _ProbeConsultingLLM:
    """Mimics a real provider: consults the ambient cancel probe.

    Real providers read the thread-local probe inside their retry
    ladders; this stub does the same via the module helper so the test
    pins that the editor service BINDS the scope on its calling thread.
    """

    def supports_structured_output(self, model=None) -> bool:
        return False

    def complete(self, prompt, **kwargs) -> str:
        from inqtrix.providers import base as providers_base

        if providers_base._provider_cancel_requested():
            raise AgentCancelled("stop")
        return '{"suggestion": "Ein Absatz."}'


def test_run_editor_suggest_binds_the_cancel_probe():
    from inqtrix.services.editor_assist_service import run_editor_suggest

    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AgentCancelled):
        run_editor_suggest(
            _suggest_request(),
            llm=_ProbeConsultingLLM(),
            model=None,
            reasoning_effort=None,
            structured_supported=False,
            timeout_seconds=5.0,
            cancel_probe=cancelled.is_set,
        )


def test_editor_suggest_route_maps_noncancelled_cancel_to_502(monkeypatch):
    """Without a disconnect, an AgentCancelled must not claim the client left."""

    class _CancelledLLM(_ProbeConsultingLLM):
        def complete(self, prompt, **kwargs) -> str:
            raise AgentCancelled("stop")

        def is_available(self) -> bool:
            return True

    providers = ProviderContext(llm=_CancelledLLM(), search=_DummySearch())
    app = create_app(settings=Settings(), providers=providers)
    with TestClient(app) as client:
        response = client.post(
            "/v1/editor/suggest",
            json={
                "block_text": "Ein Absatz.",
                "instruction": "Formeller machen.",
            },
        )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"


@pytest.mark.asyncio
async def test_editor_suggest_route_cancels_on_client_disconnect():
    """ASGI-level: a disconnect during the editor call answers 499.

    The probe-consulting LLM mimics a real provider's attempt-boundary
    check; the scripted receive channel delivers ``http.disconnect``
    after the body, and the route must map the resulting abort to 499.
    """
    import json as _json

    class _SlowProbeLLM(_ProbeConsultingLLM):
        def complete(self, prompt, **kwargs) -> str:
            from inqtrix.providers import base as providers_base

            for _ in range(200):
                if providers_base._provider_cancel_requested():
                    raise AgentCancelled("stop")
                time.sleep(0.05)
            return '{"suggestion": "nie erreicht"}'

        def is_available(self) -> bool:
            return True

    providers = ProviderContext(llm=_SlowProbeLLM(), search=_DummySearch())
    # Works without running the lifespan because the non-postgres default
    # marks the database contract ready at create_app time; a
    # postgres-backed Settings fixture would need the lifespan (503 gate).
    app = create_app(settings=Settings(), providers=providers)

    body = _json.dumps(
        {"block_text": "Ein Absatz.", "instruction": "Formeller machen."}
    ).encode()
    inbox = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict:
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(0.3)
        return {"type": "http.disconnect"}

    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/editor/suggest",
        "raw_path": b"/v1/editor/suggest",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"host", b"testserver"),
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 499
