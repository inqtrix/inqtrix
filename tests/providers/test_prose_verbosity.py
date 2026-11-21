"""The verbosity of prose: which call site asks for it and how it reaches the model.

Newer OpenAI models take a ``verbosity`` for their visible answer. A model
card may name the level its prose is written with. The resolution of a call
site names that level for a node that writes for a reader, the text calls of
the provider interface carry it, and both OpenAI-compatible adapters put it
into the request on either OpenAI API. No other call sends it, and a provider
that does not say it can send the parameter is never handed it.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion
from pydantic import ValidationError

from inqtrix import model_cards
from inqtrix.execution_authority import AuthorizationGuardedLLM
from inqtrix.model_cards import OpenAIRules
from inqtrix.model_routing import (
    NODE_TIER_ASSIGNMENT,
    PROSE_NODES,
    describe_resolution,
    describe_unresolved_resolution,
    prose_verbosity,
)
from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.providers import base as provider_base
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.base import (
    ChatTurn,
    ConfiguredLLMProvider,
    LLMProvider,
    UntrustedContentRuleProvider,
    reported_capability,
    verbosity_kwargs,
)
from inqtrix.providers.litellm import LiteLLM
from inqtrix.providers.tracing import TracingLLMProvider
from inqtrix.settings import ModelSettings

from tests.providers.test_openai_responses import _FakeClient, _failed, _text_response

_RULED = "gpt-6.1-terra"
"""On the Responses API; its card names a prose verbosity."""
_CHAT = "gpt-5.4"
"""On Chat Completions; its card names none."""
_TEXT_CALLS = ("complete", "complete_with_metadata", "chat")
_POLICY = ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768)


def _completion() -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "gpt-5.4-2026-03-05",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


def _adapter(name: str, client: _FakeClient) -> LLMProvider:
    if name == "azure":
        llm: LLMProvider = AzureOpenAILLM(
            azure_endpoint="https://t.openai.azure.com/", api_key="k", default_model=_RULED
        )
    else:
        llm = LiteLLM(api_key="k")
    llm._async_client = lambda: client  # type: ignore[attr-defined]
    return llm


def _model(adapter: str, model: str) -> str:
    return model if adapter == "azure" else f"openai/{model}"


def _invoke(llm: LLMProvider, call: str, **kwargs):
    if call == "chat":
        return llm.chat([{"role": "user", "content": "q"}], **kwargs)
    return getattr(llm, call)("q", **kwargs)


class _Plain(LLMProvider):
    """A provider written before the parameter existed: its calls take no ``verbosity``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete(  # type: ignore[override]
        self, prompt, *, system=None, model=None, max_output_tokens=None, timeout=600.0,
        state=None, deadline=None, reasoning_effort=None,
    ) -> str:
        self.calls.append({"model": model})
        return "ok"

    def is_available(self) -> bool:
        return True


class _Wordy(LLMProvider):
    """A custom provider that sends a verbosity and implements the sync calls only."""

    def __init__(self) -> None:
        self.handed: list[tuple[str, object]] = []

    def supports_verbosity(self, *, model: str | None = None) -> bool:
        return True

    def complete(self, prompt, *, verbosity=None, **kwargs) -> str:  # type: ignore[override]
        self.handed.append(("complete", verbosity))
        return "ok"

    def chat(self, messages, *, verbosity=None, **kwargs) -> ChatTurn:  # type: ignore[override]
        self.handed.append(("chat", verbosity))
        return ChatTurn(text="ok")

    def is_available(self) -> bool:
        return True


def _stack(provider: LLMProvider) -> LLMProvider:
    """Every decorator a provider passes through before a call site reaches it."""
    return AuthorizationGuardedLLM(
        UntrustedContentRuleProvider(
            TracingLLMProvider(
                ConfiguredLLMProvider(provider, ModelSettings(reasoning_model=_RULED)),
                provider_name="azure",
                policy=_POLICY,
            )
        ),
        lambda: None,
    )


# --------------------------------------------------------------------------- #
# Who asks for it
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("node", "model", "expected"),
    [
        ("answer", _RULED, "high"),
        ("agent_kernel", _RULED, "high"),
        ("answer", f"openai/{_RULED}", "high"),
        # Classifiers, planners, judges and reviewers never ask for it.
        ("classify", _RULED, ""),
        ("plan", _RULED, ""),
        ("evaluate", _RULED, ""),
        ("claim_extract", _RULED, ""),
        ("session_title", _RULED, ""),
        ("agent_deep_review", _RULED, ""),
        # Writing nodes that were not measured keep the provider default.
        ("agent_synthesis", _RULED, ""),
        ("knowledge_answer", _RULED, ""),
        ("direct_chat", _RULED, ""),
        # A writing node asks only where the card of its model names a level.
        ("answer", "gpt-6-luna", ""),
        ("answer", "gpt-6.1-sol", ""),
        ("answer", _CHAT, ""),
        ("answer", "claude-opus-4-8", ""),
        ("answer", "a-model-without-a-card", ""),
        ("answer", "", ""),
        ("answer", None, ""),
    ],
)
def test_only_a_writing_node_of_a_model_with_the_rule_asks_for_it(node, model, expected) -> None:
    assert prose_verbosity(node, model) == expected


def test_the_writing_nodes_are_known_call_sites() -> None:
    assert PROSE_NODES == {"answer", "agent_kernel"}
    assert PROSE_NODES <= set(NODE_TIER_ASSIGNMENT)


def test_the_resolution_names_the_verbosity_next_to_model_and_effort() -> None:
    """One place decides what a call site sends; every surface that shows the resolution shows this too."""
    models = SimpleNamespace(reasoning_model=_RULED, tier_fast_model="gpt-6-luna")

    assert describe_resolution("answer", models)["verbosity"] == "high"
    assert describe_resolution("agent_kernel", models)["verbosity"] == "high"
    assert describe_resolution("evaluate", models)["verbosity"] == ""
    # The model the tier resolves to decides, not the default model.
    assert describe_resolution("answer", SimpleNamespace(reasoning_model=_RULED, tier_high_model="gpt-6.1-sol"))["verbosity"] == ""
    # A model the request names explicitly goes through the same rule.
    assert describe_resolution("agent_kernel", None, requested_model=_RULED)["verbosity"] == "high"
    assert describe_resolution("agent_kernel", None, requested_model="gpt-6-luna")["verbosity"] == ""
    assert describe_resolution("direct_chat", None, requested_model=_RULED)["verbosity"] == ""


def test_a_node_without_a_resolved_model_asks_for_none() -> None:
    assert describe_unresolved_resolution("answer", None)["verbosity"] == ""


# --------------------------------------------------------------------------- #
# The card
# --------------------------------------------------------------------------- #


def test_only_a_measured_card_names_a_prose_verbosity() -> None:
    """The level is evidence per model: a card without a measurement names none."""
    naming = {
        card.id: card.openai.prose_verbosity
        for card in model_cards.MODEL_CARDS
        if card.openai is not None and card.openai.prose_verbosity is not None
    }
    assert naming == {_RULED: "high"}


def test_the_card_takes_the_three_levels_only() -> None:
    assert OpenAIRules().prose_verbosity is None
    for level in ("low", "medium", "high"):
        assert OpenAIRules(prose_verbosity=level).prose_verbosity == level
    with pytest.raises(ValidationError):
        OpenAIRules(prose_verbosity="verbose")


# --------------------------------------------------------------------------- #
# How it reaches the request
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("adapter", ["azure", "litellm"])
@pytest.mark.parametrize("call", _TEXT_CALLS)
def test_a_responses_model_gets_it_as_text_verbosity(adapter: str, call: str) -> None:
    client = _FakeClient(responses=[_text_response("Hi"), _text_response("Hi")])
    llm = _adapter(adapter, client)

    _invoke(llm, call, model=_model(adapter, _RULED), verbosity="high")
    _invoke(llm, call, model=_model(adapter, _RULED))

    assert client.responses_calls[0]["text"] == {"verbosity": "high"}
    assert "text" not in client.responses_calls[1]
    assert client.chat_calls == []


@pytest.mark.parametrize("adapter", ["azure", "litellm"])
@pytest.mark.parametrize("call", _TEXT_CALLS)
def test_a_chat_completions_model_gets_it_as_its_own_field(adapter: str, call: str) -> None:
    client = _FakeClient(completions=[_completion(), _completion()])
    llm = _adapter(adapter, client)

    _invoke(llm, call, model=_model(adapter, _CHAT), verbosity="high")
    _invoke(llm, call, model=_model(adapter, _CHAT))

    assert client.chat_calls[0]["verbosity"] == "high"
    assert "verbosity" not in client.chat_calls[1]
    assert client.responses_calls == []


def _azure_with_a_fixed_level(client: _FakeClient, model: str) -> AzureOpenAILLM:
    """An adapter whose operator fixed a verbosity for every call."""
    llm = AzureOpenAILLM(
        azure_endpoint="https://t.openai.azure.com/", api_key="k", default_model=model,
        request_params={"verbosity": "low"},
    )
    llm._async_client = lambda: client  # type: ignore[method-assign]
    return llm


@pytest.mark.parametrize("model", [_CHAT, _RULED])
@pytest.mark.parametrize("call", _TEXT_CALLS)
def test_the_value_of_the_call_wins_over_the_adapters_own_request_parameters(call: str, model: str) -> None:
    """A level fixed at the adapter goes out on every call; a call that names its own level wins."""
    client = _FakeClient(
        completions=[_completion(), _completion()], responses=[_text_response("Hi"), _text_response("Hi")]
    )
    llm = _azure_with_a_fixed_level(client, model)

    _invoke(llm, call, model=model, verbosity="high")
    _invoke(llm, call, model=model)

    if model == _CHAT:
        assert [request["verbosity"] for request in client.chat_calls] == ["high", "low"]
        assert client.responses_calls == []
    else:
        assert [request["text"] for request in client.responses_calls] == [{"verbosity": "high"}, {"verbosity": "low"}]
        assert client.chat_calls == []


@pytest.mark.parametrize("provider", [LLMProvider, AzureOpenAILLM, LiteLLM, TracingLLMProvider, ConfiguredLLMProvider])
def test_the_text_calls_take_it_and_a_structured_call_does_not(provider) -> None:
    """A structured answer has the form of its schema; nothing about its wording is asked for."""
    for name in ("complete", "complete_with_metadata", "chat", "acomplete", "acomplete_with_metadata", "achat"):
        assert "verbosity" in inspect.signature(getattr(provider, name)).parameters, name
    for name in ("complete_structured", "acomplete_structured"):
        assert "verbosity" not in inspect.signature(getattr(provider, name)).parameters, name


# --------------------------------------------------------------------------- #
# Who is handed the parameter
# --------------------------------------------------------------------------- #


def test_the_openai_adapters_say_they_send_it_and_a_plain_provider_does_not() -> None:
    assert _adapter("azure", _FakeClient()).supports_verbosity(model=_RULED) is True
    assert _adapter("litellm", _FakeClient()).supports_verbosity(model=f"openai/{_RULED}") is True
    assert reported_capability(_Plain(), "supports_verbosity", model=_RULED) is False


def test_every_wrapper_forwards_the_capability_and_the_parameter() -> None:
    """A wrapper that fell back to the base answer would silence the parameter for the whole stack."""
    client = _FakeClient(responses=[_text_response("Hi"), _text_response("Hi"), _text_response("Hi")])
    stack = _stack(_adapter("azure", client))

    assert reported_capability(stack, "supports_verbosity", model=_RULED) is True
    assert verbosity_kwargs(stack, "high", model=_RULED) == {"verbosity": "high"}
    assert verbosity_kwargs(stack, "", model=_RULED) == {}

    for call in _TEXT_CALLS:
        _invoke(stack, call, model=_RULED, **verbosity_kwargs(stack, "high", model=_RULED))

    assert [request["text"] for request in client.responses_calls] == [{"verbosity": "high"}] * 3


def test_the_async_calls_carry_it_through_the_wrappers_to_a_built_in_adapter() -> None:
    """Three wrappers have async twins of their own; the authorization guard has none and goes through the interface."""
    client = _FakeClient(responses=[_text_response("Hi") for _ in range(6)])
    inner = UntrustedContentRuleProvider(
        TracingLLMProvider(
            ConfiguredLLMProvider(_adapter("azure", client), ModelSettings(reasoning_model=_RULED)),
            provider_name="azure",
            policy=_POLICY,
        )
    )
    guarded = AuthorizationGuardedLLM(inner, lambda: None)
    for wrapper in (UntrustedContentRuleProvider, TracingLLMProvider, ConfiguredLLMProvider):
        assert "achat" in vars(wrapper), wrapper
    assert "achat" not in vars(AuthorizationGuardedLLM)

    async def calls(provider: LLMProvider) -> None:
        await provider.acomplete("q", model=_RULED, verbosity="high")
        await provider.acomplete_with_metadata("q", model=_RULED, verbosity="high")
        await provider.achat([{"role": "user", "content": "q"}], model=_RULED, verbosity="high")

    asyncio.run(calls(inner))
    asyncio.run(calls(guarded))

    assert [request["text"] for request in client.responses_calls] == [{"verbosity": "high"}] * 6


def test_a_custom_provider_that_sends_it_gets_it_through_every_wrapper() -> None:
    """A text-only provider is reached through other methods of the wrappers than a built-in adapter."""
    wordy = _Wordy()
    stack = _stack(wordy)
    inner = UntrustedContentRuleProvider(
        TracingLLMProvider(
            ConfiguredLLMProvider(wordy, ModelSettings(reasoning_model=_RULED)),
            provider_name="custom",
            policy=_POLICY,
        )
    )
    messages = [{"role": "user", "content": "q"}]

    stack.complete("q", verbosity="high")
    stack.complete_with_metadata("q", verbosity="high")
    stack.chat(messages, verbosity="high")

    async def twins() -> None:
        await inner.acomplete("q", verbosity="high")
        await inner.acomplete_with_metadata("q", verbosity="high")
        await inner.achat(messages, verbosity="high")

    asyncio.run(twins())

    assert wordy.handed == [("complete", "high"), ("complete", "high"), ("chat", "high")] * 2


def test_a_provider_that_does_not_say_so_is_never_handed_the_parameter() -> None:
    plain = _Plain()
    stack = _stack(plain)

    assert verbosity_kwargs(stack, "high", model=_RULED) == {}
    stack.complete("q", model=_RULED, **verbosity_kwargs(stack, "high", model=_RULED))
    stack.complete_with_metadata("q", model=_RULED, **verbosity_kwargs(stack, "high", model=_RULED))
    # A caller that passes the keyword without a value reaches it as well.
    stack.complete_with_metadata("q", model=_RULED, verbosity=None)

    assert plain.calls == [{"model": _RULED}] * 3


def test_the_default_methods_of_the_interface_pass_it_on() -> None:
    """A provider that implements the sync calls only is served through the base class."""
    provider = _Wordy()

    provider.complete_with_metadata("q", verbosity="high")
    asyncio.run(provider.acomplete("q", verbosity="high"))
    asyncio.run(provider.acomplete_with_metadata("q", verbosity="high"))
    asyncio.run(provider.achat([{"role": "user", "content": "q"}], verbosity="high"))

    assert provider.handed == [("complete", "high")] * 3 + [("chat", "high")]


# --------------------------------------------------------------------------- #
# What a trace says
# --------------------------------------------------------------------------- #


def _traced(llm: LLMProvider, name: str):
    """*llm* behind the tracing layer, and the exporter that keeps its spans."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    return TracingLLMProvider(llm, provider_name=name, policy=_POLICY, tracer_provider=tracer), exporter


def _model_calls(exporter) -> list:
    return [span for span in exporter.get_finished_spans() if "gen_ai.operation.name" in span.attributes]


@pytest.mark.parametrize("adapter", ["azure", "litellm"])
def test_the_trace_shows_the_verbosity_the_request_carried(adapter: str) -> None:
    client = _FakeClient(responses=[_text_response("Hi"), _text_response("Hi")])
    traced, exporter = _traced(_adapter(adapter, client), adapter)

    traced.chat([{"role": "user", "content": "q"}], model=_model(adapter, _RULED), verbosity="high")
    traced.complete_with_metadata("q", model=_model(adapter, _RULED))

    sent = [span.attributes.get("inqtrix.request.verbosity.sent") for span in _model_calls(exporter)]
    assert sent == ["high", None]


def test_the_trace_shows_a_level_fixed_at_the_adapter_on_a_call_that_asks_for_none() -> None:
    """The attribute says what the request carried, whoever put it there."""
    client = _FakeClient(responses=[_text_response("Hi")])
    traced, exporter = _traced(_azure_with_a_fixed_level(client, _RULED), "azure")

    traced.complete_with_metadata("q", model=_RULED)

    (span,) = _model_calls(exporter)
    assert span.attributes.get("inqtrix.request.verbosity.sent") == "low"
    assert client.responses_calls[0]["text"] == {"verbosity": "low"}


def test_a_repeated_attempt_carries_the_level_again_and_the_trace_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_base, "_retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    client = _FakeClient(responses=[_failed("server_error"), _text_response("Hi")])
    traced, exporter = _traced(_adapter("azure", client), "azure")

    traced.chat([{"role": "user", "content": "q"}], model=_RULED, verbosity="high")

    assert [request["text"] for request in client.responses_calls] == [{"verbosity": "high"}] * 2
    (span,) = _model_calls(exporter)
    assert span.attributes.get("inqtrix.request.verbosity.sent") == "high"
    assert span.attributes.get("inqtrix.provider.attempts") == 2
