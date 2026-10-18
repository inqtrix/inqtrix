"""Env-driven, mix-and-match provider selector (``create_providers``).

These tests pin the two independent axes (``INQTRIX_LLM_PROVIDER`` and
``INQTRIX_SEARCH_PROVIDER``), the backward-compatible default path, the
fail-loud behaviour on missing credentials (no silent fallback), and the
model-knob passthrough that feeds ``/health.models_catalog``. Explicit
constructor args are passed so the tests are independent of any ``.env``.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from inqtrix.model_routing import resolve_model
from inqtrix.providers import create_providers
from inqtrix.services.health_service import provider_label
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ProviderSettings,
    Settings,
)


def _settings(providers: ProviderSettings, *, reasoning_model: str = "R", **model_kwargs) -> Settings:
    """Build a Settings whose provider axes and model names are fully explicit."""
    return Settings(
        providers=providers,
        models=ModelSettings(reasoning_model=reasoning_model, **model_kwargs),
    )


def _capture_inqtrix_warnings(caplog: pytest.LogCaptureFixture):
    """Attach caplog to the non-propagating ``inqtrix`` logger (gotcha #2)."""
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="inqtrix")
    return logger


def test_default_unset_selectors_build_litellm_perplexity() -> None:
    providers = ProviderSettings(llm_provider="litellm", search_provider="perplexity")
    ctx = create_providers(_settings(providers, reasoning_model="gpt-4o"))
    assert provider_label(ctx.llm) == "LiteLLM"
    assert provider_label(ctx.search) == "PerplexitySearch"
    assert ctx.llm.models.reasoning_model == "gpt-4o"


def test_provider_carries_default_model_context_window_from_card() -> None:
    providers = ProviderSettings(llm_provider="litellm")
    ctx = create_providers(_settings(providers, reasoning_model="claude-opus-4-8"))
    # The provider now knows its real context window (from the model card), so
    # the editor stops assuming a tiny default and over-truncating report context
    # ("Bericht-Kontext gekuerzt"), and the graph capacity check sees the window.
    assert ctx.llm.context_window_tokens == 1_000_000


def test_provider_context_window_unset_for_uncarded_model() -> None:
    providers = ProviderSettings(llm_provider="litellm")
    ctx = create_providers(_settings(providers, reasoning_model="some-private-model"))
    # An unknown model leaves the window unset (the documented unknown-window
    # path), never a guessed default.
    assert ctx.llm.context_window_tokens is None


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        (ProviderSettings(llm_provider="litellm"), "LiteLLM"),
        (ProviderSettings(llm_provider="anthropic", anthropic_api_key="k"), "AnthropicLLM"),
        (
            ProviderSettings(
                llm_provider="azure",
                azure_openai_endpoint="https://t.openai.azure.com/",
                azure_openai_api_key="k",
            ),
            "AzureOpenAILLM",
        ),
        (ProviderSettings(llm_provider="bedrock", aws_region="eu-central-1"), "BedrockLLM"),
    ],
)
def test_llm_axis_dispatch(providers: ProviderSettings, expected: str) -> None:
    ctx = create_providers(_settings(providers))
    assert provider_label(ctx.llm) == expected


@pytest.mark.parametrize(
    ("providers", "expected"),
    [
        (ProviderSettings(search_provider="perplexity"), "PerplexitySearch"),
        (
            ProviderSettings(
                search_provider="azure_foundry",
                azure_ai_project_endpoint="https://p.services.ai.azure.com/api/projects/p",
                web_search_agent_name="web-search",
                azure_ai_project_api_key="k",
            ),
            "AzureFoundryWebSearch",
        ),
    ],
)
def test_search_axis_dispatch(providers: ProviderSettings, expected: str) -> None:
    ctx = create_providers(_settings(providers))
    assert provider_label(ctx.search) == expected


def test_mix_and_match_axes_are_independent() -> None:
    """An LLM and a search provider from different vendors compose freely."""
    providers = ProviderSettings(
        llm_provider="anthropic",
        anthropic_api_key="k",
        search_provider="azure_foundry",
        azure_ai_project_endpoint="https://p.services.ai.azure.com/api/projects/p",
        web_search_agent_name="web-search",
        azure_ai_project_api_key="k",
    )
    ctx = create_providers(_settings(providers))
    assert provider_label(ctx.llm) == "AnthropicLLM"
    assert provider_label(ctx.search) == "AzureFoundryWebSearch"


def test_search_timeout_and_foundry_concurrency_reach_provider() -> None:
    """The env/settings bridge must not fall back to provider literals."""
    providers = ProviderSettings(
        search_provider="azure_foundry",
        azure_ai_project_endpoint=(
            "https://p.services.ai.azure.com/api/projects/p"
        ),
        web_search_agent_name="web-search",
        azure_ai_project_api_key="k",
        azure_foundry_max_concurrency=7,
    )
    settings = Settings(
        providers=providers,
        models=ModelSettings(reasoning_model="R"),
        agent=AgentSettings(search_timeout=777),
    )

    ctx = create_providers(settings)

    assert ctx.search._timeout == 777
    assert ctx.search.max_search_concurrency == 7


def test_missing_anthropic_key_fails_loud(caplog: pytest.LogCaptureFixture) -> None:
    logger = _capture_inqtrix_warnings(caplog)
    providers = ProviderSettings(llm_provider="anthropic", anthropic_api_key="")
    try:
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            create_providers(_settings(providers))
    finally:
        logger.removeHandler(caplog.handler)
    assert any("ANTHROPIC_API_KEY" in r.message for r in caplog.records)


def test_missing_azure_auth_fails_loud() -> None:
    # Endpoint present but neither api key nor a complete SP trio.
    providers = ProviderSettings(
        llm_provider="azure",
        azure_openai_endpoint="https://t.openai.azure.com/",
        azure_openai_api_key="",
        azure_tenant_id="t",  # partial trio -> still must fail loudly
    )
    with pytest.raises(RuntimeError, match="AZURE_OPENAI_API_KEY"):
        create_providers(_settings(providers))


def test_missing_foundry_config_fails_loud() -> None:
    providers = ProviderSettings(
        search_provider="azure_foundry",
        azure_ai_project_endpoint="",
        web_search_agent_name="",
    )
    with pytest.raises(RuntimeError, match="AZURE_AI_PROJECT_ENDPOINT"):
        create_providers(_settings(providers))


def test_unknown_selector_rejected_at_settings_construction() -> None:
    with pytest.raises(ValidationError):
        ProviderSettings(llm_provider="bogus")
    with pytest.raises(ValidationError):
        ProviderSettings(search_provider="brave")  # reserved, not implemented


def test_model_knobs_reach_non_litellm_provider() -> None:
    """Tier models + selectable models must propagate so /health stays correct."""
    providers = ProviderSettings(
        llm_provider="anthropic",
        anthropic_api_key="k",
        selectable_chat_models=["claude-opus-4-8", "claude-sonnet-4-6"],
    )
    settings = _settings(
        providers,
        reasoning_model="claude-opus-4-8",
        tier_high_model="claude-opus-4-8",
        tier_fast_model="claude-haiku-4-5",
    )
    ctx = create_providers(settings)
    assert resolve_model("answer", ctx.llm.models) == "claude-opus-4-8"
    assert resolve_model("classify", ctx.llm.models) == "claude-haiku-4-5"
    assert ctx.llm.selectable_models == ["claude-opus-4-8", "claude-sonnet-4-6"]


def test_selectable_models_split_from_env_string() -> None:
    providers = ProviderSettings(
        llm_provider="litellm",
        selectable_chat_models="gpt-4o, gpt-4o-mini ,,gpt-5",
    )
    ctx = create_providers(_settings(providers))
    assert ctx.llm.selectable_models == ["gpt-4o", "gpt-4o-mini", "gpt-5"]


def test_providers_none_defaults_to_litellm_perplexity() -> None:
    """The defensive ``_sel`` fallback: a settings object without a populated
    ``providers`` group still builds the historical LiteLLM+Perplexity stack."""
    settings = Settings.model_construct(
        providers=None,
        server=Settings.model_fields["server"].annotation.model_construct(
            litellm_base_url="http://localhost:4000/v1",
            litellm_api_key="k",
            perplexity_api_key="k",
            perplexity_base_url="",
        ),
        models=ModelSettings(reasoning_model="gpt-4o"),
        agent=Settings.model_fields["agent"].annotation.model_construct(
            search_cache_maxsize=256, search_cache_ttl=3600
        ),
    )
    ctx = create_providers(settings)
    assert provider_label(ctx.llm) == "LiteLLM"
    assert provider_label(ctx.search) == "PerplexitySearch"
    assert ctx.llm.selectable_models == []  # empty catalogue not passed -> provider default


def test_empty_selectable_string_is_empty_list() -> None:
    assert ProviderSettings(selectable_chat_models="").selectable_chat_models == []
    assert ProviderSettings().selectable_chat_models == []


def test_blank_temperature_env_is_treated_as_unset() -> None:
    # "present but empty" must not crash Settings() construction.
    assert ProviderSettings(temperature="").temperature is None
    assert ProviderSettings(temperature="0.7").temperature == pytest.approx(0.7)


def test_perplexity_empty_key_allowed_at_factory_time() -> None:
    """Perplexity stays lenient (historical first-call-failure contract):
    an empty key builds the provider without a startup error, unlike the
    fail-loud behaviour for a selected anthropic/azure/bedrock provider."""
    settings = Settings(
        providers=ProviderSettings(search_provider="perplexity"),
        models=ModelSettings(reasoning_model="R"),
        server=Settings.model_fields["server"].annotation.model_construct(
            litellm_base_url="http://localhost:4000/v1",
            litellm_api_key="k",
            perplexity_api_key="",
            perplexity_base_url="",
        ),
    )
    ctx = create_providers(settings)
    assert provider_label(ctx.search) == "PerplexitySearch"


def test_inapplicable_temperature_warns_for_litellm(caplog: pytest.LogCaptureFixture) -> None:
    logger = _capture_inqtrix_warnings(caplog)
    providers = ProviderSettings(llm_provider="litellm", temperature=0.5)
    try:
        ctx = create_providers(_settings(providers))
    finally:
        logger.removeHandler(caplog.handler)
    assert provider_label(ctx.llm) == "LiteLLM"
    assert any("INQTRIX_TEMPERATURE" in r.message for r in caplog.records)


def test_absent_provider_group_falls_back_to_the_field_default() -> None:
    """The no-group fallback must BE the field's default, not equal it.

    Checked at the call site, because value equality proves nothing here:
    a literal ``6`` restored at the call site still equals the field's
    current default, so an assertion on the value alone goes green on
    exactly the regression it names. What must hold is that the fallback
    is derived, so it follows the field the day the field changes.
    """
    import ast
    import inspect

    from inqtrix import providers as module

    tree = ast.parse(inspect.getsource(module._make_azure_foundry))
    fallbacks = [
        node.args[2]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_sel"
        and len(node.args) == 3
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "azure_foundry_max_concurrency"
    ]
    assert len(fallbacks) == 1, "expected exactly one concurrency lookup"
    fallback = fallbacks[0]
    assert not isinstance(fallback, ast.Constant), (
        "the fallback is a literal again: it is a second default that keeps "
        "the old number the day the field changes"
    )
    assert (
        isinstance(fallback, ast.Call)
        and isinstance(fallback.func, ast.Name)
        and fallback.func.id == "_field_default"
    )


def test_the_derived_fallback_follows_the_field(monkeypatch) -> None:
    """And it must follow a CHANGED default, which is the whole point."""
    from inqtrix.providers import _field_default
    from inqtrix.settings import ProviderSettings

    attr = "azure_foundry_max_concurrency"
    field = ProviderSettings.model_fields[attr]
    monkeypatch.setattr(field, "default", 4321)
    assert _field_default(attr) == 4321
