"""Tier configuration via provider constructors and the env bridge.

Each provider accepts tier / per-node model args and forwards them into its
internal ``ModelSettings`` (read by the runtime through ``providers.llm.models``
and resolved by ``inqtrix.model_routing``). ``create_providers`` carries the env
tier fields from ``Settings`` into the constructed provider.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from inqtrix.model_routing import resolve_effort, resolve_model
from inqtrix.providers import create_providers
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.providers.litellm import LiteLLM
from inqtrix.settings import ModelSettings, Settings


def _azure(**kwargs) -> AzureOpenAILLM:
    return AzureOpenAILLM(azure_endpoint="https://t.openai.azure.com/", api_key="k", **kwargs)


def _anthropic(**kwargs) -> AnthropicLLM:
    return AnthropicLLM(api_key="k", **kwargs)


def _bedrock(**kwargs) -> BedrockLLM:
    return BedrockLLM(**kwargs)


def _litellm(**kwargs) -> LiteLLM:
    return LiteLLM(api_key="k", **kwargs)


def test_anthropic_constructor_routes_tiers_and_overrides() -> None:
    llm = AnthropicLLM(
        api_key="k",
        default_model="R",
        tier_high_model="H",
        tier_mid_model="M",
        tier_fast_model="F",
        tier_high_effort="medium",
        answer_model="OVERRIDE",
    )
    assert resolve_model("answer", llm.models) == "OVERRIDE"  # per-node beats tier
    assert resolve_model("plan", llm.models) == "M"
    assert resolve_model("evaluate", llm.models) == "M"
    assert resolve_model("classify", llm.models) == "F"
    assert resolve_model("direct_chat", llm.models) == "M"
    assert resolve_effort("answer", llm.models) == "medium"
    assert resolve_effort("classify", llm.models) == ""  # fast tier effort unset


def test_azure_constructor_routes_tiers() -> None:
    llm = _azure(default_model="R", tier_high_model="gpt5", tier_fast_model="gpt5-mini")
    assert resolve_model("answer", llm.models) == "gpt5"
    assert resolve_model("classify", llm.models) == "gpt5-mini"


def test_litellm_constructor_routes_tiers() -> None:
    llm = LiteLLM(api_key="k", default_model="R", tier_mid_model="lmid")
    assert resolve_model("plan", llm.models) == "lmid"
    assert resolve_model("evaluate", llm.models) == "lmid"


def test_bedrock_claim_extract_uses_fast_tier_when_set() -> None:
    # Regression: pinning claim_extract to default_model would shadow the
    # fast tier on Bedrock's highest-volume node.
    llm = BedrockLLM(default_model="R", tier_fast_model="bfast")
    assert resolve_model("claim_extract", llm.models) == "bfast"


@pytest.mark.parametrize("factory", [_azure, _anthropic, _bedrock, _litellm])
def test_provider_constructors_expose_direct_chat_tiers(
    factory: Callable[..., Any],
) -> None:
    llm = factory(
        default_model="R",
        tier_high_model="H",
        tier_mid_model="M",
        tier_fast_model="F",
        tier_high_effort="medium",
        tier_mid_effort="none",
        tier_fast_effort="low",
    )

    assert resolve_model("direct_chat", llm.models, requested_tier="high") == "H"
    assert resolve_effort("direct_chat", llm.models, requested_tier="high") == "medium"
    assert resolve_model("direct_chat", llm.models, requested_tier="mid") == "M"
    assert resolve_effort("direct_chat", llm.models, requested_tier="mid") == "none"
    assert resolve_model("direct_chat", llm.models, requested_tier="fast") == "F"
    assert resolve_effort("direct_chat", llm.models, requested_tier="fast") == "low"


@pytest.mark.parametrize("factory", [_azure, _anthropic, _bedrock, _litellm])
def test_provider_constructors_keep_direct_chat_override_authoritative(
    factory: Callable[..., Any],
) -> None:
    llm = factory(
        default_model="R",
        direct_chat_model="PINNED",
        tier_high_model="H",
        tier_mid_model="M",
        tier_fast_model="F",
        tier_fast_effort="none",
    )

    assert resolve_model("direct_chat", llm.models, requested_tier="fast") == "PINNED"
    assert resolve_effort("direct_chat", llm.models, requested_tier="fast") == "none"


def test_bedrock_claim_extract_falls_back_to_reasoning_without_tier() -> None:
    llm = BedrockLLM(default_model="R")
    assert resolve_model("claim_extract", llm.models) == "R"
    assert llm.models.effective_claim_extract_model == "R"


def test_bedrock_explicit_claim_extract_override_wins() -> None:
    llm = BedrockLLM(default_model="R", claim_extract_model="explicit", tier_fast_model="bfast")
    assert resolve_model("claim_extract", llm.models) == "explicit"


def test_no_tiers_means_everything_uses_reasoning_model() -> None:
    # Backward-compat: with nothing but default_model set, every node resolves
    # to it and effort stays unset (inherit provider default).
    llm = LiteLLM(api_key="k", default_model="only")
    for node in ("classify", "plan", "evaluate", "answer", "claim_extract", "direct_chat"):
        assert resolve_model(node, llm.models) == "only"
        assert resolve_effort(node, llm.models) == ""


def test_create_providers_maps_env_tier_fields() -> None:
    settings = Settings(models=ModelSettings(
        reasoning_model="R",
        tier_high_model="H",
        tier_mid_model="M",
        tier_fast_model="F",
        tier_high_effort="high",
    ))
    providers = create_providers(settings)
    models = providers.llm.models
    assert resolve_model("answer", models) == "H"
    assert resolve_model("plan", models) == "M"
    assert resolve_model("claim_extract", models) == "F"
    assert resolve_effort("answer", models) == "high"
