"""OpenAI request rules: "off" goes out where a model knows it, and no effort beyond what its API carries.

GPT-6 deployments think at ``medium`` when the request carries no
``reasoning_effort`` and refuse ``max`` over Chat Completions ("Supported values
are: 'none', 'low', 'medium', 'high', and 'xhigh'").
"""

from __future__ import annotations

import logging

import pytest

from inqtrix import model_cards
from inqtrix.execution_authority import AuthorizationGuardedLLM
from inqtrix.model_cards import build_models_catalog
from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.providers import _openai_rules
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.base import (
    ConfiguredLLMProvider,
    UntrustedContentRuleProvider,
    validate_reasoning_effort,
)
from inqtrix.providers.litellm import LiteLLM
from inqtrix.providers.tracing import TracingLLMProvider
from inqtrix.settings import ModelSettings

_TERRA_LEVELS = ["none", "low", "medium", "high", "xhigh", "max"]
"""GPT-6.1 Terra is on the Responses API, which carries every effort of its card."""
_CHAT_LEVELS = ["none", "low", "medium", "high", "xhigh"]
"""What Chat Completions can send for a card that knows ``max``."""


@pytest.fixture(autouse=True)
def _fresh_warnings():
    _openai_rules._warned.clear()
    yield
    _openai_rules._warned.clear()


def _azure() -> AzureOpenAILLM:
    return AzureOpenAILLM(
        azure_endpoint="https://t.openai.azure.com/", api_key="k", default_model="gpt-6.1-terra"
    )


def _litellm_effort(effort: str, model: str) -> dict[str, object]:
    kwargs: dict[str, object] = {}
    LiteLLM._apply_reasoning_effort(kwargs, effort, model=model)
    return kwargs


def test_off_goes_out_as_none_where_the_card_knows_it() -> None:
    """Leaving the field out would run GPT-6 at its default ``medium``."""
    out = _azure()._apply_call_reasoning_effort(
        {"model": "gpt-6.1-terra", "reasoning_effort": "medium"}, "none", use_model="gpt-6.1-terra"
    )
    assert out["reasoning_effort"] == "none"
    assert _litellm_effort("none", "openai/gpt-6-luna") == {"reasoning_effort": "none"}


@pytest.mark.parametrize("model", ["gpt-4o", "gpt-4.1", "claude-opus-4-8"])
def test_off_stays_out_where_no_openai_card_knows_none(model: str) -> None:
    """No card, a card without reasoning, or another vendor behind the gateway: no field, as before."""
    out = _azure()._apply_call_reasoning_effort(
        {"model": model, "reasoning_effort": "medium"}, "none", use_model=model
    )
    assert "reasoning_effort" not in out
    assert _litellm_effort("none", model) == {}


def test_max_where_the_model_has_no_max_runs_at_xhigh_with_one_warning(caplog) -> None:
    """A per-run or skill ``max`` on a Chat Completions model runs at the strongest level below it, said once."""
    az = _azure()
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers._openai_rules"):
        first = az._apply_call_reasoning_effort(
            {"model": "gpt-5.4", "temperature": 0.2}, "max", use_model="gpt-5.4"
        )
        second = az._apply_call_reasoning_effort({}, "max", use_model="gpt-5.4")
    assert first["reasoning_effort"] == second["reasoning_effort"] == "xhigh"
    assert "temperature" not in first
    warnings = [r.getMessage() for r in caplog.records if "gpt-5.4" in r.getMessage()]
    assert len(warnings) == 1
    assert "runs at 'xhigh'" in warnings[0]
    assert _litellm_effort("max", "openai/gpt-5.4") == {"reasoning_effort": "xhigh"}


def test_a_responses_model_keeps_max() -> None:
    """The Responses API takes ``max``, so the call carries it."""
    out = _azure()._apply_call_reasoning_effort({}, "max", use_model="gpt-6.1-terra")
    assert out["reasoning_effort"] == "max"


def test_a_graded_level_goes_through_unchanged_for_models_without_openai_rules() -> None:
    """The gateway decides for every other model, and rejects what it cannot honor visibly."""
    assert _litellm_effort("max", "claude-opus-4-8") == {"reasoning_effort": "max"}
    assert _litellm_effort("high", "gpt-4o") == {"reasoning_effort": "high"}


@pytest.mark.parametrize(
    ("value", "supported", "expected"),
    [
        ("max", ("minimal", "low", "medium", "high", "xhigh"), "xhigh"),
        ("xhigh", ("low", "medium"), "medium"),
        ("minimal", ("low", "medium", "high", "xhigh", "max"), "none"),
        ("bogus", ("low", "medium"), "none"),
        ("high", ("low", "high"), "high"),
    ],
)
def test_an_unsupported_level_becomes_the_strongest_allowed_one_below_it(
    value: str, supported: tuple[str, ...], expected: str
) -> None:
    """Never more reasoning than asked for, as close to it as the model allows."""
    effort, warnings = validate_reasoning_effort(value, supported_levels=supported)
    assert effort == expected
    assert bool(warnings) is (value not in supported)


@pytest.fixture
def chat_card_with_max(monkeypatch):
    """A Chat Completions card that lists ``max``, as a model card could; the rules and the catalogue both see it."""
    real = model_cards.resolve_model_card

    def resolve(model: str):
        card = real(model)
        if card is not None and card.id == "gpt-5.4":
            return card.model_copy(update={"reasoning_levels": [*_CHAT_LEVELS, "max"]})
        return card

    monkeypatch.setattr(_openai_rules, "resolve_model_card", resolve)
    monkeypatch.setattr(model_cards, "resolve_model_card", resolve)


def test_the_levels_are_those_the_models_api_can_send(chat_card_with_max) -> None:
    az = _azure()
    assert az.reasoning_levels(model="gpt-5.4") == _CHAT_LEVELS
    assert az.reasoning_levels(model="gpt-6.1-terra") == _TERRA_LEVELS
    assert az.reasoning_levels(model="my-gpt4o-deployment") is None
    assert LiteLLM(api_key="k").reasoning_levels(model="claude-opus-4-8") is None


def test_the_catalogue_offers_only_the_levels_the_provider_can_send(chat_card_with_max) -> None:
    az = _azure()
    offered = build_models_catalog(["gpt-5.4", "gpt-6.1-terra", "unknown-deployment"], az.reasoning_levels)
    assert offered[0]["card"]["reasoning_levels"] == _CHAT_LEVELS
    assert offered[1]["card"]["reasoning_levels"] == _TERRA_LEVELS
    assert offered[2]["card"] is None
    card_only = build_models_catalog(["gpt-5.4"])
    assert card_only[0]["card"]["reasoning_levels"] == [*_CHAT_LEVELS, "max"]


def test_the_catalogue_shows_the_levels_card_and_provider_share_in_the_cards_order() -> None:
    """A provider level the card does not list never appears, and the card's order stands."""
    offered = build_models_catalog(["gpt-5.4"], lambda *, model: ["xhigh", "low", "extreme"])
    assert offered[0]["card"]["reasoning_levels"] == ["low", "xhigh"]


def test_azure_offers_no_max_for_a_card_without_openai_rules() -> None:
    """Azure sends at most ``xhigh`` for such a model, so the picker must not offer ``max``."""
    card = model_cards.resolve_model_card("claude-opus-4-8")
    assert card is not None and card.openai is None and "max" in card.reasoning_levels
    levels = _azure().reasoning_levels(model="claude-opus-4-8")
    assert levels == [level for level in card.reasoning_levels if level != "max"]


def test_every_wrapper_forwards_the_levels() -> None:
    """A wrapper that fell back to the base default would put ``max`` back into the picker."""
    az = _azure()
    policy = ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768)
    stack = AuthorizationGuardedLLM(
        UntrustedContentRuleProvider(
            TracingLLMProvider(
                ConfiguredLLMProvider(az, ModelSettings(reasoning_model="gpt-6.1-terra")),
                provider_name="azure",
                policy=policy,
            )
        ),
        lambda: None,
    )
    assert stack.reasoning_levels(model="gpt-6.1-terra") == _TERRA_LEVELS


@pytest.mark.parametrize(("model", "weakest"), [("gpt-6.1-sol", "low"), ("gpt-5.5-pro", "medium")])
def test_off_runs_at_the_weakest_level_where_the_model_cannot_switch_off(model: str, weakest: str, caplog) -> None:
    """A missing field would run the model at its default; the weakest level is the closest to "off" it gets."""
    assert "none" not in model_cards.resolve_model_card(model).reasoning_levels
    assert model_cards.resolve_model_card(model).reasoning_levels[0] == weakest
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers._openai_rules"):
        off = _azure()._apply_call_reasoning_effort({"temperature": 0.2}, "none", use_model=model)
        below = _azure()._apply_call_reasoning_effort({}, "minimal", use_model=model)
    assert off["reasoning_effort"] == below["reasoning_effort"] == weakest
    assert "temperature" not in off
    messages = [r.getMessage() for r in caplog.records]
    assert any("cannot be switched off" in message and model in message for message in messages)
    assert not any("reasoning disabled" in message for message in messages)
    assert _litellm_effort("none", f"openai/{model}") == {"reasoning_effort": weakest}


def test_a_step_below_the_weakest_level_turns_reasoning_off_where_the_card_knows_none() -> None:
    out = _azure()._apply_call_reasoning_effort({}, "minimal", use_model="gpt-6.1-terra")
    assert out["reasoning_effort"] == "none"


def test_a_configured_model_without_a_card_is_named_once_at_start(monkeypatch, caplog) -> None:
    from types import SimpleNamespace

    from inqtrix import providers

    monkeypatch.setattr(providers, "_warned_without_card", set())
    settings = SimpleNamespace(models=ModelSettings(reasoning_model="prod-terra", tier_fast_model="gpt-6-luna"))
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers"):
        providers._common_llm_model_kwargs(settings, None)
        providers._common_llm_model_kwargs(settings, None)
    named = [r.getMessage() for r in caplog.records if "has no model card" in r.getMessage()]
    assert len(named) == 1
    assert "'prod-terra'" in named[0]
    assert "gpt-6-luna" not in named[0]


def test_the_trace_shows_the_effort_asked_for_and_the_one_sent() -> None:
    """``max`` on a Chat Completions model runs at ``xhigh``; the generation says both."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from openai.types.chat import ChatCompletion

    from tests.providers.test_openai_responses import _FakeClient

    completion = ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "gpt-5.4-2026-03-05",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Hi"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    client = _FakeClient(completions=[completion])
    az = _azure()
    az._async_client = lambda: client  # type: ignore[method-assign]
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    traced = TracingLLMProvider(
        az, provider_name="azure",
        policy=ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768), tracer_provider=tracer,
    )

    traced.chat([{"role": "user", "content": "q"}], model="gpt-5.4", reasoning_effort="max")

    assert client.chat_calls[0]["reasoning_effort"] == "xhigh"
    (span,) = [span for span in exporter.get_finished_spans() if "inqtrix.request.reasoning_effort" in span.attributes]
    assert span.attributes["inqtrix.request.reasoning_effort"] == "max"
    assert span.attributes["inqtrix.request.reasoning_effort.sent"] == "xhigh"
