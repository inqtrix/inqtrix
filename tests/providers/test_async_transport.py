"""The async transport of the built-in adapters: one loop, one attempt per request.

The adapters reach a real ``AsyncOpenAI`` client whose HTTP transport is an
``httpx.MockTransport``: every request the SDK sends is counted here, so a
retry hidden inside the SDK would show.
"""

from __future__ import annotations

import asyncio
import gc
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from inqtrix import aio
from inqtrix.exceptions import AgentProviderTimeout
from inqtrix.providers import base as provider_base
from inqtrix.providers import litellm as litellm_module
from inqtrix.providers.attempts import (
    AsyncNativeLLMProvider,
    AttemptInfo,
    AttemptOutcome,
    bind_attempt_observer,
    call_with_retries,
    count_attempts,
    observe_retries,
    provider_attempt,
)
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.providers.base import ProviderAttemptBudget, provider_attempt_scope
from inqtrix.providers.litellm import LiteLLM
from inqtrix.providers.rerankers import CohereRerank, RerankerError
from inqtrix.providers import azure as azure_module
from inqtrix.providers import azure_web_search as foundry_module
from inqtrix.providers import perplexity as perplexity_module
from inqtrix.providers.azure_web_search import AzureFoundryWebSearch
from inqtrix.providers.perplexity import PerplexitySearch
from inqtrix.exceptions import AgentModelCapacityError
from openai import APIStatusError

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]


def _completion(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        },
    )


def _error(status: int) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "busy", "code": str(status)}})


class Gateway:
    """Answers the requests of one test in order and counts them."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


def _litellm(handler: Handler) -> LiteLLM:
    llm = LiteLLM(api_key="test-key", base_url="http://gateway.test/v1", default_model="m")
    llm._client_kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_base, "_retry_delay_seconds", lambda *_args: 0.0)


def test_each_request_is_one_attempt_of_the_one_loop() -> None:
    """Two 503 then an answer: three requests, three attempts, two retries reported."""
    gateway = Gateway(_error(503), _error(503), _completion())
    llm = _litellm(gateway)
    budget = ProviderAttemptBudget()
    notices: list[dict[str, Any]] = []

    with provider_attempt_scope(budget), observe_retries("model", notices.append):
        response = llm.complete_with_metadata("Frage")

    assert response.content == "ok"
    assert len(gateway.requests) == 3
    assert budget.used == 3
    assert [notice["status_code"] for notice in notices] == [503, 503]


def test_a_rate_limit_is_an_attempt_like_any_other() -> None:
    gateway = Gateway(_error(429), _completion())
    llm = _litellm(gateway)
    budget = ProviderAttemptBudget()

    with provider_attempt_scope(budget):
        assert llm.complete("Frage") == "ok"

    assert len(gateway.requests) == 2
    assert budget.used == 2


def test_an_attempt_past_its_deadline_claims_nothing() -> None:
    """The deadline check comes before the claim: a request never sent is no attempt."""
    budget = ProviderAttemptBudget()

    info = AttemptInfo("", "Probe", "m", "chat", 1, 0, None)

    async def attempt() -> None:
        async with provider_attempt(
            info, label="Probe", deadline=time.monotonic() - 1.0, budget=budget
        ):
            raise AssertionError("an expired attempt must not run")

    with count_attempts() as count, pytest.raises(AgentProviderTimeout):
        asyncio.run(attempt())
    assert budget.used == 0
    assert count.value == 0


def test_one_client_serves_every_call_of_an_adapter_on_a_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = Gateway(_completion(), _completion(), _completion())
    llm = _litellm(gateway)
    built: list[Any] = []
    real = litellm_module.AsyncOpenAI
    monkeypatch.setattr(
        litellm_module, "AsyncOpenAI", lambda **kwargs: built.append(kwargs) or real(**kwargs)
    )

    llm.complete("eins")
    llm.complete("zwei")
    assert len(built) == 1
    assert len(llm._clients) == 1

    async def on_another_loop() -> int:
        await llm.acomplete("drei")
        return len(llm._clients)

    assert asyncio.run(on_another_loop()) == 2
    assert len(built) == 2
    gc.collect()
    assert len(llm._clients) == 1, "a closed loop's client must go with it"


def test_cancelling_the_waiting_task_cancels_the_request() -> None:
    started, cancelled = asyncio.Event(), threading.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _completion()

    llm = _litellm(hanging)

    async def main() -> bool:
        call = asyncio.ensure_future(llm.acomplete("Frage"))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # Read before asyncio.run cancels whatever is left on its way out.
        return cancelled.is_set()

    assert asyncio.run(main())


def test_the_operation_deadline_ends_a_hanging_request_as_its_timeout() -> None:
    """The transport here never times out on its own; the attempt's deadline does."""

    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return _completion()

    llm = _litellm(hanging)
    started = time.monotonic()

    with pytest.raises(AgentProviderTimeout):
        llm.complete("Frage", timeout=0.3)

    assert time.monotonic() - started < 2.0


@pytest.mark.refuses_the_bridge_on_purpose
def test_a_sync_face_refuses_on_the_loop_it_would_wait_for() -> None:
    llm = _litellm(Gateway(_completion()))

    async def on_the_loop() -> str:
        return llm.complete("Frage")

    with pytest.raises(aio.CalledOnLoopThread):
        aio.call_from_thread(on_the_loop())


def test_an_adapter_without_an_async_method_cannot_be_built() -> None:
    """A missing twin would fall back to the base class and wait on its own sync face."""

    class Partial(AsyncNativeLLMProvider):
        async def acomplete(self, prompt: str, **kwargs: Any) -> str:
            return ""

        async def acomplete_with_metadata(self, prompt: str, **kwargs: Any) -> Any:
            return None

        async def acomplete_structured(self, prompt: str, **kwargs: Any) -> Any:
            return None

        def is_available(self) -> bool:
            return True

    with pytest.raises(TypeError, match="achat"):
        Partial()
    assert issubclass(AzureOpenAILLM, AsyncNativeLLMProvider)
    assert issubclass(LiteLLM, AsyncNativeLLMProvider)


def test_the_clients_never_retry_on_their_own() -> None:
    assert LiteLLM(api_key="k", base_url="http://gateway.test/v1")._client_kwargs["max_retries"] == 0
    azure = AzureOpenAILLM(azure_endpoint="https://test.openai.azure.com/", api_key="k")
    assert azure._client_kwargs["max_retries"] == 0
    assert json.dumps(azure._client_kwargs["base_url"]) == '"https://test.openai.azure.com/openai/v1/"'


def _ranked(*indices: int) -> httpx.Response:
    return httpx.Response(
        200, json={"results": [{"index": index, "relevance_score": 0.5} for index in indices]}
    )


def _cohere(handler: Handler, *, timeout: float = 60.0) -> CohereRerank:
    reranker = CohereRerank(
        api_key="rerank-key",
        base_url="https://rerank.example",
        default_model="rerank-v3.5",
        timeout=timeout,
    )
    reranker._client_kwargs["transport"] = httpx.MockTransport(handler)
    return reranker


def test_each_rerank_request_is_one_attempt_of_the_one_loop() -> None:
    gateway = Gateway(_error(503), _ranked(0))
    reranker = _cohere(gateway)
    budget = ProviderAttemptBudget()
    notices: list[dict[str, Any]] = []

    with provider_attempt_scope(budget), observe_retries("rerank", notices.append):
        assert [result.index for result in reranker.rerank("Frage", ["eins"], top_n=1)] == [0]

    assert len(gateway.requests) == 2
    assert budget.used == 2
    assert [notice["status_code"] for notice in notices] == [503]


def test_a_rerank_transport_error_is_retried() -> None:
    requests: list[httpx.Request] = []

    async def flaky(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _ranked(0)

    assert [result.index for result in _cohere(flaky).rerank("Frage", ["eins"], top_n=1)] == [0]
    assert len(requests) == 2


def test_one_client_serves_every_rerank_of_an_adapter_on_a_loop() -> None:
    gateway = Gateway(_ranked(0), _ranked(0))
    reranker = _cohere(gateway)

    reranker.rerank("eins", ["a"], top_n=1)
    reranker.rerank("zwei", ["a"], top_n=1)

    assert len(gateway.requests) == 2
    assert len(reranker._clients) == 1


def test_the_rerank_deadline_ends_a_hanging_request_as_a_rerank_error() -> None:
    """The transport never times out on its own; the attempt's deadline does."""

    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return _ranked(0)

    started = time.monotonic()

    with pytest.raises(RerankerError, match="operation time limit"):
        _cohere(hanging, timeout=0.3).rerank("Frage", ["eins"], top_n=1)

    assert time.monotonic() - started < 2.0


def _message(text: str = "ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 2},
        },
    )


def _anthropic(handler: Handler) -> AnthropicLLM:
    llm = AnthropicLLM(api_key="test-key", default_model="claude-sonnet-4-6")
    llm._client_kwargs["transport"] = httpx.MockTransport(handler)
    return llm


def test_each_anthropic_request_is_one_attempt_of_the_one_loop() -> None:
    """500, the 529 overload, then an answer: three requests, three attempts."""
    gateway = Gateway(_error(500), _error(529), _message())
    llm = _anthropic(gateway)
    budget = ProviderAttemptBudget()
    notices: list[dict[str, Any]] = []

    with provider_attempt_scope(budget), observe_retries("model", notices.append):
        assert llm.complete("Frage") == "ok"

    assert len(gateway.requests) == 3
    assert budget.used == 3
    assert [notice["status_code"] for notice in notices] == [500, 529]


def test_one_client_serves_every_anthropic_call_on_a_loop() -> None:
    gateway = Gateway(_message(), _message(), _message())
    llm = _anthropic(gateway)

    for prompt in ("eins", "zwei", "drei"):
        assert llm.complete(prompt) == "ok"

    assert len(gateway.requests) == 3
    assert len(llm._clients) == 1


def test_an_anthropic_transport_error_is_retried() -> None:
    requests: list[httpx.Request] = []

    async def flaky(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _message()

    assert _anthropic(flaky).complete("Frage") == "ok"
    assert len(requests) == 2


def test_the_operation_deadline_ends_a_hanging_anthropic_request() -> None:
    async def hanging(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        return _message()

    started = time.monotonic()

    with pytest.raises(AgentProviderTimeout):
        _anthropic(hanging).complete("Frage", timeout=0.3)

    assert time.monotonic() - started < 2.0


def test_the_anthropic_adapter_sends_without_urllib() -> None:
    import ast
    import inspect

    from inqtrix.providers import anthropic as anthropic_module

    tree = ast.parse(inspect.getsource(anthropic_module))
    imported = {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}

    assert not [name for name in imported if name.startswith("urllib")]


def _converse(text: str = "ok") -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 3, "outputTokens": 2},
    }


def _throttled(code: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": "busy"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
        "Converse",
    )


def _bedrock(converse: Callable[..., Any]) -> BedrockLLM:
    llm = BedrockLLM(region_name="eu-central-1", default_model="eu.anthropic.claude-sonnet-4-6")
    client = SimpleNamespace(converse=converse)
    llm._client_for_deadline = lambda deadline: (client, 60.0)  # type: ignore[method-assign]
    return llm


def test_each_bedrock_call_is_one_attempt_on_the_provider_lane() -> None:
    answers = [_throttled("ServiceUnavailableException"), _converse()]
    threads: list[str] = []

    def converse(**params: Any) -> dict[str, Any]:
        threads.append(threading.current_thread().name)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    budget = ProviderAttemptBudget()
    notices: list[dict[str, Any]] = []

    with provider_attempt_scope(budget), observe_retries("model", notices.append):
        assert _bedrock(converse).complete("Frage") == "ok"

    assert len(threads) == 2
    assert all(name.startswith(f"inqtrix-{aio.PROVIDER_LANE}") for name in threads)
    assert budget.used == 2
    assert [notice["error_code"] for notice in notices] == ["ServiceUnavailableException"]


def test_the_operation_deadline_ends_the_wait_for_a_hanging_bedrock_call() -> None:
    """The boto3 thread runs on until its read timeout; the caller does not wait for it."""

    def converse(**params: Any) -> dict[str, Any]:
        time.sleep(1.5)
        return _converse()

    started = time.monotonic()

    with pytest.raises(AgentProviderTimeout):
        _bedrock(converse).complete("Frage", timeout=0.3)

    assert time.monotonic() - started < 1.2


def _traced(provider: Any) -> tuple[Any, Any]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.providers.tracing import instrument_llm

    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    wrapped = instrument_llm(
        provider,
        provider_name="test",
        policy=ContentCapturePolicy(configured_capture=False, max_attr_bytes=32768),
        tracer_provider=tracer,
    )
    return wrapped, exporter


def test_the_generation_counts_the_requests_of_its_call() -> None:
    """One generation per logical call; its attempts equal the requests sent."""
    from inqtrix.observability import semconv

    gateway = Gateway(_error(503), _error(503), _completion())
    traced, exporter = _traced(_litellm(gateway))

    assert traced.complete("Frage") == "ok"

    (span,) = exporter.get_finished_spans()
    assert span.attributes[semconv.INQTRIX_PROVIDER_ATTEMPTS] == len(gateway.requests) == 3
    assert span.attributes[semconv.observation_metadata_key("attempts")] == 3


def test_a_provider_outside_the_attempt_hook_reports_no_attempts() -> None:
    from inqtrix.observability import semconv
    from inqtrix.providers.base import LLMProvider, LLMResponse

    class SyncOnly(LLMProvider):
        def complete(self, prompt: str, **kwargs: Any) -> str:
            return "ok"

        def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
            return LLMResponse("ok", 1, 1, "m", "stop", {})

        def is_available(self) -> bool:
            return True

    traced, exporter = _traced(SyncOnly())

    assert traced.complete_with_metadata("Frage").content == "ok"

    (span,) = exporter.get_finished_spans()
    assert semconv.INQTRIX_PROVIDER_ATTEMPTS not in span.attributes


def test_thinking_reports_the_raised_output_budget_not_the_callers() -> None:
    """Anthropic raises max_tokens for thinking; the generation shows what was sent."""
    from inqtrix.observability import semconv
    from inqtrix.providers._claude import _THINKING_MIN_MAX_TOKENS

    sent: list[dict[str, Any]] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _message()

    llm = AnthropicLLM(
        api_key="test-key", default_model="claude-sonnet-4-6", thinking={"type": "adaptive"}
    )
    llm._client_kwargs["transport"] = httpx.MockTransport(gateway)
    traced, exporter = _traced(llm)

    traced.complete_with_metadata("Frage", max_output_tokens=1000)

    (span,) = exporter.get_finished_spans()
    assert sent[0]["max_tokens"] == _THINKING_MIN_MAX_TOKENS > 1000
    assert span.attributes[semconv.GEN_AI_REQUEST_MAX_TOKENS] == _THINKING_MIN_MAX_TOKENS


class _Recorder:
    """An attempt observer that writes down what it sees."""

    def __init__(self) -> None:
        self.infos: list[AttemptInfo] = []
        self.outcomes: list[AttemptOutcome] = []

    def before_attempt(self, info: AttemptInfo) -> object:
        self.infos.append(info)
        return info.attempt

    def after_attempt(self, ticket: object, outcome: AttemptOutcome) -> None:
        assert ticket == len(self.outcomes) + 1
        self.outcomes.append(outcome)


def _probe_call(send: Callable[[], Awaitable[str]], *, model: str = "m", seconds: float = 5.0) -> Awaitable[str]:
    return call_with_retries(
        send,
        provider_label="Probe",
        model=model,
        operation="chat",
        deadline=time.monotonic() + seconds,
        outer_deadline=None,
        timeout_label="Probe",
    )


def test_observers_see_each_attempt_and_how_it_ended() -> None:
    """503, 503, 200: three attempts, two announced retries, then success."""
    from inqtrix.observability.context import bind_purpose

    gateway = Gateway(_error(503), _error(503), _completion())
    llm = _litellm(gateway)
    seen = _Recorder()

    with bind_purpose("classify"), bind_attempt_observer(seen):
        assert llm.complete("Frage", max_output_tokens=321) == "ok"

    sent = json.loads(gateway.requests[0].content)
    assert [info.attempt for info in seen.infos] == [1, 2, 3]
    assert {(info.purpose, info.provider, info.model) for info in seen.infos} == {("classify", "LiteLLM", "m")}
    assert seen.infos[0].max_output_tokens == sent[llm._token_budget_parameter] == 321
    assert seen.infos[0].estimated_input_tokens > 0
    assert [outcome.status for outcome in seen.outcomes] == ["failed", "failed", "ok"]
    assert [(outcome.retry or {}).get("status_code") for outcome in seen.outcomes] == [503, 503, None]


def test_the_attempt_carries_the_output_budget_actually_sent() -> None:
    """Anthropic raises 1,000 to the thinking minimum; the attempt reports the sent value."""
    from inqtrix.providers._claude import _THINKING_MIN_MAX_TOKENS

    sent: list[dict[str, Any]] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _message()

    llm = AnthropicLLM(
        api_key="test-key", default_model="claude-sonnet-4-6", thinking={"type": "adaptive"}
    )
    llm._client_kwargs["transport"] = httpx.MockTransport(gateway)
    seen = _Recorder()

    with bind_attempt_observer(seen):
        llm.complete_with_metadata("Frage", max_output_tokens=1000)

    assert sent[0]["max_tokens"] == _THINKING_MIN_MAX_TOKENS > 1000
    assert [info.max_output_tokens for info in seen.infos] == [_THINKING_MIN_MAX_TOKENS]


def test_azure_and_bedrock_report_the_budget_they_send() -> None:
    gateway = Gateway(_completion())
    azure = AzureOpenAILLM(azure_endpoint="https://test.openai.azure.com/", api_key="k", default_model="m")
    azure._client_kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(gateway))
    sent_params: list[dict[str, Any]] = []

    def converse(**params: Any) -> dict[str, Any]:
        sent_params.append(params)
        return _converse()

    azure_seen, bedrock_seen = _Recorder(), _Recorder()
    with bind_attempt_observer(azure_seen):
        azure.complete("Frage", max_output_tokens=222)
    with bind_attempt_observer(bedrock_seen):
        _bedrock(converse).complete("Frage", max_output_tokens=333)

    azure_sent = json.loads(gateway.requests[0].content)
    assert azure_seen.infos[0].max_output_tokens == azure_sent[azure._token_budget_parameter] == 222
    assert bedrock_seen.infos[0].max_output_tokens == sent_params[0]["inferenceConfig"]["maxTokens"] == 333


def test_an_attempt_the_deadline_cuts_off_is_unknown() -> None:
    """The request may have reached the provider: neither ok nor failed."""
    seen = _Recorder()

    async def hang() -> str:
        await asyncio.sleep(5)
        return "late"

    with bind_attempt_observer(seen), pytest.raises(AgentProviderTimeout):
        asyncio.run(_probe_call(hang, seconds=0.05))

    assert [outcome.status for outcome in seen.outcomes] == ["unknown"]


def test_a_failing_observer_never_breaks_the_call(caplog: pytest.LogCaptureFixture) -> None:
    after_calls: list[AttemptOutcome] = []

    class Broken:
        def before_attempt(self, info: AttemptInfo) -> object:
            raise RuntimeError("observer bug")

        def after_attempt(self, ticket: object, outcome: AttemptOutcome) -> None:
            after_calls.append(outcome)

    async def answer() -> str:
        return "ok"

    seen = _Recorder()
    with bind_attempt_observer(Broken()), bind_attempt_observer(seen):
        assert asyncio.run(_probe_call(answer)) == "ok"

    assert after_calls == []
    assert [outcome.status for outcome in seen.outcomes] == ["ok"]
    assert "Provider attempt observer failed before the request" in caplog.text


def test_two_calls_on_one_loop_see_only_their_own_attempts() -> None:
    first, second = _Recorder(), _Recorder()

    async def one(name: str, seen: _Recorder) -> str:
        async def send() -> str:
            await asyncio.sleep(0.01)
            return name

        with bind_attempt_observer(seen):
            return await _probe_call(send, model=name)

    async def both() -> list[str]:
        return list(await asyncio.gather(one("a", first), one("b", second)))

    assert asyncio.run(both()) == ["a", "b"]
    assert [info.model for info in first.infos] == ["a"]
    assert [info.model for info in second.infos] == ["b"]



def test_a_server_error_that_names_its_wait_is_retried_after_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 with Retry-After waits as told, like a 429."""
    asked: list[str | None] = []

    def delay(attempt: int, retry_after: str | None = None) -> float:
        asked.append(retry_after)
        return 0.0

    monkeypatch.setattr(provider_base, "_retry_delay_seconds", delay)
    busy = httpx.Response(503, headers={"retry-after": "2"}, json={"error": {"message": "busy", "code": "503"}})

    assert _litellm(Gateway(busy, _completion())).complete("Frage") == "ok"
    assert asked == ["2"]


def test_a_cancelled_run_closes_the_request_it_waits_for() -> None:
    """The waiting caller ends the request at once instead of waiting for its answer."""
    from inqtrix.exceptions import AgentCancelled
    from inqtrix.providers.base import provider_cancel_scope

    cancelled = threading.Event()
    closed = threading.Event()

    async def hang(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(5)
        finally:
            closed.set()
        return _completion()

    seen = _Recorder()
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    started = time.monotonic()
    with provider_cancel_scope(cancelled.is_set), bind_attempt_observer(seen), pytest.raises(AgentCancelled):
        _litellm(hang).complete("Frage")
    timer.cancel()

    assert time.monotonic() - started < 3
    assert closed.wait(2)
    assert [outcome.status for outcome in seen.outcomes] == ["unknown"]


# ---------------------------------------------------------------------------
# The transport rules hold for every adapter, not only LiteLLM
# ---------------------------------------------------------------------------


def _search_answer() -> httpx.Response:
    return httpx.Response(200, json={
        "id": "resp_1", "object": "response", "created_at": 0, "model": "m", "status": "completed",
        "output": [], "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
    })


def _mock_client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _azure(handler: Handler) -> AzureOpenAILLM:
    llm = AzureOpenAILLM(azure_endpoint="https://test.openai.azure.com/", api_key="k", default_model="m")
    llm._client_kwargs["http_client"] = _mock_client(handler)
    return llm


def _perplexity(handler: Handler) -> PerplexitySearch:
    search = PerplexitySearch(api_key="k")
    search._client_kwargs["http_client"] = _mock_client(handler)
    return search


def _foundry(handler: Handler) -> AzureFoundryWebSearch:
    search = AzureFoundryWebSearch(
        project_endpoint="https://test.ai.azure.com/api/projects/p", agent_name="web-search-agent", api_key="k"
    )
    search._client_kwargs["http_client"] = _mock_client(handler)
    return search


# adapter: (build, module of its SDK client, client class name, one call, a good answer)
_ADAPTERS: dict[str, tuple[Any, Any, str, Callable[[Any, int], Any], Callable[[], httpx.Response]]] = {
    "azure": (_azure, azure_module, "AsyncOpenAI", lambda llm, index: llm.complete(f"Frage {index}"), _completion),
    "perplexity": (
        _perplexity, perplexity_module, "AsyncPerplexity", lambda search, index: search.search(f"Frage {index}"),
        _search_answer,
    ),
    "foundry": (
        _foundry, foundry_module, "AsyncOpenAI", lambda search, index: search.search(f"Frage {index}"), _search_answer,
    ),
}


@pytest.mark.parametrize("adapter", sorted(_ADAPTERS))
def test_every_adapter_keeps_one_client_over_its_calls(adapter: str, monkeypatch: pytest.MonkeyPatch) -> None:
    build, module, client_class, call, answer = _ADAPTERS[adapter]
    gateway = Gateway(answer(), answer(), answer())
    provider = build(gateway)
    built: list[Any] = []
    real = getattr(module, client_class)
    monkeypatch.setattr(module, client_class, lambda **kwargs: built.append(kwargs) or real(**kwargs))

    for index in range(3):
        call(provider, index)

    assert len(gateway.requests) == 3
    assert len(built) == 1


@pytest.mark.parametrize("adapter", sorted(_ADAPTERS))
def test_every_adapter_sends_one_request_per_attempt(adapter: str) -> None:
    """A server error is retried by the one loop, never by the SDK underneath it."""
    build, _module, _client_class, call, answer = _ADAPTERS[adapter]
    gateway = Gateway(_error(500), answer())
    provider = build(gateway)
    budget = ProviderAttemptBudget()

    with provider_attempt_scope(budget):
        call(provider, 0)

    assert len(gateway.requests) == budget.used == 2


def _anthropic(handler: Handler) -> AnthropicLLM:
    llm = AnthropicLLM(api_key="test-key", default_model="claude-sonnet-4-6")
    llm._client_kwargs["transport"] = httpx.MockTransport(handler)
    return llm


@pytest.mark.parametrize("build", [_litellm, _anthropic], ids=["litellm", "anthropic"])
def test_a_cancelled_call_closes_its_request_and_ends_as_unknown(build: Callable[[Handler], Any]) -> None:
    """Anthropic sends through its own httpx client; both paths must pass the cancellation on."""
    started, cancelled = asyncio.Event(), threading.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return _completion()

    llm = build(hanging)
    seen = _Recorder()

    async def main() -> bool:
        with bind_attempt_observer(seen):
            call = asyncio.ensure_future(llm.acomplete("Frage"))
        await started.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        # Read before asyncio.run cancels whatever is left on its way out.
        return cancelled.is_set()

    assert asyncio.run(main())
    assert [outcome.status for outcome in seen.outcomes] == ["unknown"]


def test_observers_see_a_rate_limit_and_a_last_failed_attempt() -> None:
    """429, 503, 503: three attempts, the last one fails for good and announces no retry."""
    gateway = Gateway(_error(429), _error(503), _error(503))
    llm = _litellm(gateway)
    seen = _Recorder()

    with bind_attempt_observer(seen), pytest.raises((APIStatusError, AgentModelCapacityError)):
        llm.complete("Frage")

    assert [info.attempt for info in seen.infos] == [1, 2, 3]
    assert [outcome.status for outcome in seen.outcomes] == ["failed", "failed", "failed"]
    assert [(outcome.retry or {}).get("status_code") for outcome in seen.outcomes] == [429, 503, None]


def test_a_chat_generation_shows_the_output_budget_it_sent() -> None:
    """A chat answer does not repeat the budget; the attempt that sent it does."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.providers.tracing import TracingLLMProvider

    gateway = Gateway(_completion())
    llm = _litellm(gateway)
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    traced = TracingLLMProvider(
        llm,
        provider_name="litellm",
        policy=ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768),
        tracer_provider=tracer,
    )

    traced.chat([{"role": "user", "content": "Frage"}])

    sent = json.loads(gateway.requests[0].content)[llm._token_budget_parameter]
    (span,) = exporter.get_finished_spans()
    assert span.attributes["gen_ai.request.max_tokens"] == sent > 0


def test_a_wait_past_the_deadline_is_not_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Retry-After longer than the time left ends the call with its own error at once."""
    monkeypatch.setattr(provider_base, "_retry_delay_seconds", lambda attempt, retry_after=None: 60.0)
    busy = httpx.Response(503, headers={"retry-after": "60"}, json={"error": {"message": "busy", "code": "503"}})
    gateway = Gateway(busy, _completion())
    started = time.monotonic()

    with pytest.raises((APIStatusError, AgentModelCapacityError)):
        _litellm(gateway).complete("Frage", timeout=5.0)

    assert time.monotonic() - started < 1.0
    assert len(gateway.requests) == 1


def test_a_failing_cancel_probe_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    """A waiting call asks the probe ten times a second; one stack trace is enough."""
    import logging

    from inqtrix.providers.base import provider_cancel_scope

    def broken() -> bool:
        raise RuntimeError("probe broke")

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return _completion()

    with caplog.at_level(logging.DEBUG, logger="inqtrix.providers.base"), provider_cancel_scope(broken):
        assert _litellm(slow).complete("Frage") == "ok"

    failed = [record for record in caplog.records if record.getMessage().startswith("Provider cancel probe failed")]
    assert sum(record.levelno == logging.ERROR for record in failed) == 1
    assert len(failed) > 1


def test_importing_the_package_loads_the_client_resources_the_adapters_call() -> None:
    """The OpenAI client imports a resource's modules lazily, at its first call.

    That first call runs on the event loop every run and request shares: about
    40 ms for chat completions and 60 ms for responses, once per process. A
    fresh interpreter shows what importing the package alone has loaded (it
    imports every adapter).
    """
    script = (
        "import sys\n"
        "import inqtrix\n"
        "print([name in sys.modules for name in "
        "('openai.resources.chat.completions', 'openai.resources.responses')])\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "[True, True]"

