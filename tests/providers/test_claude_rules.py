"""What each Claude model accepts in a request, on Anthropic and on Bedrock alike."""

from __future__ import annotations

import logging

import pytest

from inqtrix.providers import _claude
from inqtrix.providers._claude import thinking_and_effort
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.bedrock import BedrockLLM


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch) -> None:
    monkeypatch.setattr(_claude, "_warned", set())


def _resolve(effort: str, model: str) -> tuple[object, object]:
    return thinking_and_effort(
        effort, model=model, inherit_thinking=None, inherit_effort=None, label="Test"
    )


def test_none_on_a_model_that_cannot_stop_thinking_runs_low_effort_with_one_warning(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        first = _resolve("none", "claude-opus-5-5")
        second = _resolve("none", "eu.anthropic.claude-opus-5-5-v1:0")

    assert first == second == ({"type": "adaptive"}, "low")
    assert caplog.text.count("thinking cannot be switched off") == 1


def test_none_switches_thinking_off_explicitly_where_the_model_thinks_by_default() -> None:
    assert _resolve("none", "claude-opus-5") == ({"type": "disabled"}, None)
    assert _resolve("none", "anthropic.claude-haiku-5-5") == ({"type": "disabled"}, None)


def test_none_leaves_thinking_out_where_the_model_does_not_think_by_default() -> None:
    assert _resolve("none", "claude-opus-4-8") == (None, None)


def test_a_graded_effort_on_a_fixed_budget_model_runs_without_thinking(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert _resolve("high", "claude-haiku-4-5") == (None, None)

    assert "takes only a fixed thinking budget" in caplog.text


def test_haiku_5_5_takes_an_effort() -> None:
    assert _resolve("medium", "claude-haiku-5-5") == ({"type": "adaptive"}, "medium")


def test_a_model_without_a_card_keeps_the_rules_from_before_the_cards() -> None:
    assert _resolve("high", "claude-haiku-3-9") == ({"type": "adaptive"}, None)
    assert _resolve("none", "claude-unknown-9") == (None, None)


def test_a_temperature_a_configured_model_refuses_stops_the_anthropic_start() -> None:
    with pytest.raises(ValueError, match="claude-opus-5-5"):
        AnthropicLLM(api_key="k", default_model="claude-opus-4-6", tier_high_model="claude-opus-5-5", temperature=0.2)


def test_a_temperature_a_configured_model_refuses_stops_the_bedrock_start() -> None:
    with pytest.raises(ValueError, match="anthropic.claude-sonnet-5-5"):
        BedrockLLM(default_model="anthropic.claude-sonnet-5-5", temperature=0.2)


def test_a_temperature_every_configured_model_takes_is_kept() -> None:
    llm = AnthropicLLM(
        api_key="k",
        default_model="claude-opus-4-6",
        claim_extract_model="claude-haiku-4-5",
        temperature=0.2,
    )
    assert llm._temperature == 0.2


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1", "claude-opus-4-8"],
)
def test_the_current_models_offer_structured_output(model: str) -> None:
    assert AnthropicLLM(api_key="k").supports_structured_output(model=model)
    assert BedrockLLM(default_model="anthropic.claude-opus-4-6").supports_structured_output(
        model=f"anthropic.{model}"
    )
