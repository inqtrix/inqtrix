"""The async provider surface: sync-only providers, and wrappers that stay async inside."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.providers.tracing import (
    TracingLLMProvider,
    TracingSearchProvider,
)
from inqtrix.providers import base as provider_base
from inqtrix.providers.base import (
    ChatTurn,
    ConfiguredLLMProvider,
    LLMProvider,
    LLMResponse,
    SearchProvider,
    StructuredLLMResponse,
    UntrustedContentRuleProvider,
)
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import ModelSettings
from tests._tracing_helpers import in_memory_provider

_LLM_TWINS = ("acomplete", "acomplete_with_metadata", "acomplete_structured", "achat")
_SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
_MESSAGES = [{"role": "system", "content": "Sei knapp."}, {"role": "user", "content": "Frage"}]

# (sync method, positional arguments, keyword arguments) as callers pass them
_LLM_CALLS = [
    ("complete", ("Frage",), {"system": "Sei knapp.", "reasoning_effort": "low"}),
    ("complete_with_metadata", ("Frage",), {"system": "Sei knapp.", "max_output_tokens": 50}),
    (
        "complete_structured",
        ("Frage",),
        {"schema": _SCHEMA, "schema_name": "Antwort", "system": "Sei knapp."},
    ),
    ("chat", (_MESSAGES,), {"tools": [], "tool_choice": "required", "reasoning_effort": "high"}),
]


@pytest.fixture(autouse=True)
def nobody_named_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts before any class was named for its sync-only async calls."""
    monkeypatch.setattr(provider_base, "_SYNC_ONLY_CLASSES", set())


def _result(method: str, prompt: str) -> Any:
    if method == "complete":
        return f"Text zu {prompt}"
    if method == "complete_with_metadata":
        return LLMResponse(content=f"Text zu {prompt}", model="m", prompt_tokens=3)
    if method == "complete_structured":
        return StructuredLLMResponse(parsed={"ok": True}, content="{}", model="m")
    return ChatTurn(text="Antwort", finish_reason="stop", model="m")


class SyncOnly(LLMProvider):
    """A custom provider of the old kind: sync methods only."""

    def __init__(self) -> None:
        self.threads: list[str] = []

    def _answer(self, method: str, prompt: Any) -> Any:
        self.threads.append(threading.current_thread().name)
        return _result(method, str(prompt))

    def complete(self, prompt: str, **kwargs: Any) -> str:
        return self._answer("complete", prompt)

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        return self._answer("complete_with_metadata", prompt)

    def complete_structured(self, prompt: str, **kwargs: Any) -> StructuredLLMResponse:
        return self._answer("complete_structured", prompt)

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ChatTurn:
        return self._answer("chat", messages)

    def is_available(self) -> bool:
        return True


class OldSignature(LLMProvider):
    """A provider whose ``complete`` predates the effort parameter."""

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = 600.0,
        state: dict | None = None,
        deadline: float | None = None,
    ) -> str:
        return "alt"

    def is_available(self) -> bool:
        return True


class SyncSearch(SearchProvider):
    def __init__(self) -> None:
        self.threads: list[str] = []

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        self.threads.append(threading.current_thread().name)
        return GroundedSearchResult(answer=f"Treffer zu {query}")

    def is_available(self) -> bool:
        return True


class Recording(LLMProvider):
    """Records every call: which entry it reached, with which arguments."""

    complete_uses_metadata = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...], dict[str, Any]]] = []

    def _answer(self, entry: str, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        self.calls.append((entry, method, args, kwargs))
        return _result(method, str(args[0]))

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return self._answer("sync", "complete", args, kwargs)

    def complete_with_metadata(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return self._answer("sync", "complete_with_metadata", args, kwargs)

    def complete_structured(self, *args: Any, **kwargs: Any) -> StructuredLLMResponse:
        return self._answer("sync", "complete_structured", args, kwargs)

    def chat(self, *args: Any, **kwargs: Any) -> ChatTurn:
        return self._answer("sync", "chat", args, kwargs)

    async def acomplete(self, *args: Any, **kwargs: Any) -> str:
        return self._answer("async", "complete", args, kwargs)

    async def acomplete_with_metadata(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return self._answer("async", "complete_with_metadata", args, kwargs)

    async def acomplete_structured(self, *args: Any, **kwargs: Any) -> StructuredLLMResponse:
        return self._answer("async", "complete_structured", args, kwargs)

    async def achat(self, *args: Any, **kwargs: Any) -> ChatTurn:
        return self._answer("async", "chat", args, kwargs)

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return True

    def is_available(self) -> bool:
        return True


def _configured(inner: LLMProvider) -> LLMProvider:
    return ConfiguredLLMProvider(inner, ModelSettings(reasoning_model="standard-modell"))


def _with_rule(inner: LLMProvider) -> LLMProvider:
    return UntrustedContentRuleProvider(inner)


def _traced(inner: LLMProvider) -> LLMProvider:
    tracer, _ = in_memory_provider()
    return TracingLLMProvider(
        inner,
        provider_name="test",
        policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
        tracer_provider=tracer,
    )


@pytest.mark.parametrize(("method", "args", "kwargs"), _LLM_CALLS)
def test_a_sync_only_provider_serves_the_async_twin_on_the_provider_lane(
    method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    provider = SyncOnly()

    result = asyncio.run(getattr(provider, f"a{method}")(*args, **kwargs))

    assert result == _result(method, str(args[0]))
    assert len(provider.threads) == 1
    assert provider.threads[0].startswith("inqtrix-provider")


def test_a_sync_only_search_serves_its_async_twin_on_the_provider_lane() -> None:
    provider = SyncSearch()

    result = asyncio.run(provider.asearch("Modellgerät", recency_filter="week"))

    assert result.answer == "Treffer zu Modellgerät"
    assert provider.threads[0].startswith("inqtrix-provider")


def test_a_sync_only_class_is_named_once(caplog: pytest.LogCaptureFixture) -> None:
    async def calls() -> None:
        await SyncOnly().acomplete("a")
        await SyncOnly().achat(_MESSAGES)
        await SyncSearch().asearch("b")

    with caplog.at_level(logging.WARNING, logger="inqtrix.providers.base"):
        asyncio.run(calls())

    named = [record.getMessage() for record in caplog.records if "has no async" in record.getMessage()]
    assert len(named) == 2
    assert named[0].startswith("CONFIG: SyncOnly has no async complete")
    assert named[1].startswith("CONFIG: SyncSearch has no async search")


def test_a_sync_method_that_predates_a_parameter_still_serves_the_async_twin() -> None:
    assert asyncio.run(OldSignature().acomplete("Frage")) == "alt"


@pytest.mark.parametrize(
    ("wrapper", "twins"),
    [
        (ConfiguredLLMProvider, _LLM_TWINS),
        (UntrustedContentRuleProvider, _LLM_TWINS),
        (TracingLLMProvider, _LLM_TWINS),
        (TracingSearchProvider, ("asearch",)),
    ],
)
def test_a_wrapper_implements_every_async_twin_itself(wrapper: type, twins: tuple[str, ...]) -> None:
    """A twin left to the base class would run the wrapper's sync method on a thread."""
    assert [name for name in twins if name not in vars(wrapper)] == []


@pytest.mark.parametrize("wrap", [_configured, _with_rule, _traced], ids=["configured", "rule", "traced"])
@pytest.mark.parametrize(("method", "args", "kwargs"), _LLM_CALLS, ids=[call[0] for call in _LLM_CALLS])
def test_a_wrapper_forwards_the_async_twin_as_it_forwards_the_sync_call(
    wrap: Any, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    sync_inner, async_inner = Recording(), Recording()

    sync_result = getattr(wrap(sync_inner), method)(*args, **kwargs)
    async_result = asyncio.run(getattr(wrap(async_inner), f"a{method}")(*args, **kwargs))

    assert [call[0] for call in sync_inner.calls] == ["sync"]
    assert [call[0] for call in async_inner.calls] == ["async"]
    assert async_inner.calls[0][1:] == sync_inner.calls[0][1:]
    if isinstance(sync_result, str):
        assert async_result == sync_result
    else:
        assert async_result.model == sync_result.model


def test_the_traced_twin_exports_the_span_of_the_sync_call() -> None:
    tracer, exporter = in_memory_provider()

    def traced() -> LLMProvider:
        return TracingLLMProvider(
            Recording(),
            provider_name="test",
            policy=ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
            tracer_provider=tracer,
        )

    traced().chat(_MESSAGES, tools=[], tool_choice="required")
    asyncio.run(traced().achat(_MESSAGES, tools=[], tool_choice="required"))

    sync_span, async_span = exporter.get_finished_spans()
    assert async_span.name == sync_span.name == "chat"
    assert dict(async_span.attributes) == dict(sync_span.attributes)
