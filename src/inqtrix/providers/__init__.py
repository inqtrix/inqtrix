"""Provider package — factory for the env-selected provider stack.

This package contains the abstract provider contracts (in ``base``),
concrete LLM providers (``litellm``, ``anthropic``, ``azure``,
``bedrock``), and concrete search providers (``perplexity``,
``azure_web_search``).

The :func:`create_providers` factory lives here because it instantiates
concrete providers from sub-modules — placing it in ``base`` would
create a circular import. The factory is the single env -> constructor
bridge: it reads :class:`~inqtrix.settings.Settings` and builds the LLM
provider selected by ``INQTRIX_LLM_PROVIDER`` and the search provider
selected by ``INQTRIX_SEARCH_PROVIDER`` (two independent axes). Providers
themselves never read environment variables (constructor-first rule).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from openai import OpenAI

if TYPE_CHECKING:
    from inqtrix.providers.base import LLMProvider, ProviderContext, SearchProvider
    from inqtrix.settings import ProviderSettings, Settings

log = logging.getLogger("inqtrix")


def create_providers(settings: "Settings") -> "ProviderContext":
    """Create the provider pair selected by the env-driven axes.

    Use this factory when the caller did not inject explicit provider
    instances into ``AgentConfig`` and the runtime should instead build
    the stack from environment-backed settings. ``settings.providers``
    selects the LLM axis (``litellm`` | ``anthropic`` | ``azure`` |
    ``bedrock``) and the search axis (``perplexity`` | ``azure_foundry``)
    independently, so any LLM pairs with any search provider. The mapping
    from ``Settings`` to provider constructors lives here in one place so
    the auto-create path is predictable and free of duplicated wiring.

    Backward compatibility: the ``ProviderSettings`` defaults
    (``litellm``/``perplexity``, empty catalogue) reproduce the historical
    auto-create stack, so an ordinary ``Settings()`` with no provider env
    set builds the LiteLLM-plus-Perplexity pair with byte-identical
    constructor arguments. If a caller hands in ``settings.providers=None``
    (or a settings object without the group), ``_sel`` falls back to those
    same defaults.

    No silent fallback: when a *selected* non-default provider is missing a
    required credential the factory raises (and logs a warning) at startup
    rather than degrading to a different provider. Knobs that the selected
    provider cannot honour (e.g. ``temperature`` under ``litellm``) are
    reported with a visible warning, never dropped silently.

    Args:
        settings: Fully resolved runtime settings. ``settings.providers``
            supplies the axis selectors, per-provider credentials, and
            construction knobs; ``settings.models`` supplies model names and
            tiers; ``settings.server`` supplies the LiteLLM/Perplexity
            endpoints and keys; ``settings.agent`` supplies search-cache
            sizing.

    Returns:
        ProviderContext: A provider container with the selected LLM in
        ``llm`` and the selected search provider in ``search``.

    Raises:
        RuntimeError: If a selected provider is missing a required
            credential (anthropic/azure/bedrock/azure_foundry).

    Example:
        >>> from inqtrix.providers import create_providers
        >>> from inqtrix.settings import Settings
        >>> settings = Settings.model_construct(
        ...     server=Settings.model_fields["server"].annotation.model_construct(
        ...         litellm_base_url="http://localhost:4000/v1",
        ...         litellm_api_key="test-key",
        ...         perplexity_api_key="test-key",
        ...     ),
        ...     models=Settings.model_fields["models"].annotation.model_construct(
        ...         reasoning_model="gpt-4o",
        ...         classify_model="",
        ...         claim_extract_model="gpt-4o-mini",
        ...         evaluate_model="",
        ...         search_model="",
        ...     ),
        ...     agent=Settings.model_fields["agent"].annotation.model_construct(
        ...         search_cache_maxsize=256,
        ...         search_cache_ttl=3600,
        ...     ),
        ... )
        >>> providers = create_providers(settings)
        >>> providers.llm.models.reasoning_model
        'gpt-4o'
    """
    from inqtrix.providers.base import MAX_PROVIDER_ATTEMPTS, ProviderContext

    log.info(
        "CONFIG: AI operation policy reasoning=%ss editor=%ss search=%ss "
        "claim_extract=%ss research_run=%ss max_attempts=%s",
        settings.agent.reasoning_timeout,
        settings.agent.editor_assistant_timeout,
        settings.agent.search_timeout,
        settings.agent.claim_extract_timeout,
        settings.agent.max_total_seconds,
        MAX_PROVIDER_ATTEMPTS,
    )

    providers_cfg = getattr(settings, "providers", None)
    llm = _build_llm_provider(settings, providers_cfg)
    search = _build_search_provider(settings, providers_cfg)

    return instrument_providers(
        ProviderContext(llm=llm, search=search),
        settings,
        provider_name=_sel(providers_cfg, "llm_provider", "litellm"),
    )


def instrument_providers(
    providers: ProviderContext,
    settings: "Settings",
    *,
    provider_name: str = "custom",
) -> ProviderContext:
    """Instrument a provider context and publish the content policy.

    ONE wrap point for every LLM and search call in the codebase
    (duration_ms on the response DTOs, gen_ai spans, call metrics,
    ledger rows, error visibility). Applies to BOTH origins — providers
    this module builds and providers a caller injects — because
    telemetry that depends on how the object was constructed is
    telemetry an operator cannot rely on.

    The same argument makes it the place for the untrusted-content rule:
    a guarantee that holds only for providers built one way is not a
    guarantee. The rule decorator sits OUTSIDE the tracing decorator:
    tracing records the prompt it is handed, so only a rule attached
    before it reaches the trace — and a trace that hides the rule makes a
    covered call look exactly like a bypass.

    Idempotent: each wrapper looks along the whole ``_provider`` chain
    and refuses to add a second layer, so calling this on an
    already-instrumented context — or on a provider an integrator traced
    beforehand — is a no-op. Without the observability extra the wrappers
    degrade to duration measurement.

    The factories that build or accept providers pass through here
    (:func:`create_providers`, ``create_app``, ``create_multi_stack_app``
    and the library route in ``inqtrix.agent``). The lower injection seams
    (``register_routes``, ``build_container``) take providers as given and
    expect them instrumented.
    """
    from inqtrix.observability.content import (
        build_content_policy,
        set_active_content_policy,
    )
    from inqtrix.observability.provider_tracing import (
        instrument_llm,
        instrument_search,
    )
    from inqtrix.providers.base import (
        ProviderContext,
        with_untrusted_content_rule_provider,
    )

    policy = build_content_policy(settings)
    # Publish for deep call sites that cannot be threaded a policy (the
    # kernel tool boundary) — the whole process gates content on ONE
    # decision, built from the COMPOSED settings.
    set_active_content_policy(policy)
    llm = providers.llm
    search = providers.search
    if llm is not None:
        llm = with_untrusted_content_rule_provider(
            instrument_llm(llm, provider_name=provider_name, policy=policy)
        )
    if search is not None:
        search = instrument_search(search, policy=policy)
    return ProviderContext(llm=llm, search=search)


def _field_default(attr: str) -> Any:
    """Read a provider field's own default instead of restating it.

    A literal repeated at a call site is a second default: it keeps the old
    value silently the day the field changes.
    """
    from inqtrix.settings import ProviderSettings

    return ProviderSettings.model_fields[attr].default


def _sel(providers_cfg: "ProviderSettings | None", attr: str, default: Any) -> Any:
    """Read a provider-settings field, defaulting when the group is absent.

    A real ``Settings()`` always carries a populated ``providers`` group (its
    ``default_factory`` runs even under ``model_construct``), and the group's
    own defaults already reproduce the historical stack. This guard only
    matters when a caller explicitly passes ``settings.providers=None`` (or a
    settings object that never set the attribute): the absent value then
    falls back to the historical default rather than raising ``AttributeError``.
    """
    if providers_cfg is None:
        return default
    return getattr(providers_cfg, attr, default)


def _require(value: str, env_name: str, provider: str) -> str:
    """Return ``value`` or fail loudly (no silent fallback to another provider).

    Args:
        value: The resolved credential/setting.
        env_name: The env-var name to name in the error and warning.
        provider: The selected provider value, for the operator-facing message.

    Raises:
        RuntimeError: If ``value`` is empty.
    """
    if not value:
        log.warning(
            "CONFIG: selected provider '%s' requires %s but it is empty; "
            "failing loudly (no fallback to another provider).",
            provider,
            env_name,
        )
        raise RuntimeError(f"Selected provider '{provider}' requires {env_name}.")
    return value


def _warn_ignored(value: Any, env_name: str, provider: str) -> None:
    """Log a visible warning when a knob is set but the provider ignores it."""
    if value not in (None, "", []):
        log.warning(
            "CONFIG: %s is set but provider '%s' does not honour it; ignoring.",
            env_name,
            provider,
        )


def _common_llm_model_kwargs(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> dict[str, Any]:
    """Build the model-name/tier/picker kwargs shared by every LLM provider.

    Every concrete LLM constructor accepts this identical block, so role
    models, tier models, per-tier effort, and the selectable-model catalogue
    reach the *selected* provider — not just LiteLLM as before.
    """
    m = settings.models
    kwargs: dict[str, Any] = {
        "default_model": m.reasoning_model,
        "classify_model": m.classify_model,
        "claim_extract_model": m.claim_extract_model,
        "evaluate_model": m.evaluate_model,
        "plan_model": m.plan_model,
        "answer_model": m.answer_model,
        "direct_chat_model": m.direct_chat_model,
        "tier_high_model": m.tier_high_model,
        "tier_mid_model": m.tier_mid_model,
        "tier_fast_model": m.tier_fast_model,
        "tier_high_effort": m.tier_high_effort,
        "tier_mid_effort": m.tier_mid_effort,
        "tier_fast_effort": m.tier_fast_effort,
    }
    # The provider's coarse context-window size comes from the default model's
    # card so budget logic (editor context windowing, the graph capacity check)
    # uses the real window instead of a tiny fallback. Without this the editor
    # assumed a 16k window and routinely truncated report context ("Bericht-
    # Kontext gekuerzt"). An unknown model leaves it unset (the documented
    # unknown-window warning path); test stubs are built directly, not here.
    from inqtrix.model_cards import resolve_model_card

    default_card = resolve_model_card(m.reasoning_model)
    if default_card is not None:
        kwargs["context_window_tokens"] = default_card.context_window_tokens
    # Only forward optional knobs when actually set, like temperature /
    # token_budget below — so the default path passes the exact constructor
    # arguments the historical create_providers did (byte-identical), while a
    # configured catalogue still reaches every provider.
    selectable = _sel(providers_cfg, "selectable_chat_models", [])
    if selectable:
        kwargs["selectable_models"] = list(selectable)
    return kwargs


def _build_llm_provider(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "LLMProvider":
    """Dispatch the LLM axis selector to the matching ``_make_*`` builder."""
    selector = _sel(providers_cfg, "llm_provider", "litellm")
    if selector == "litellm":
        return _make_litellm(settings, providers_cfg)
    if selector == "anthropic":
        return _make_anthropic(settings, providers_cfg)
    if selector == "azure":
        return _make_azure_openai(settings, providers_cfg)
    if selector == "bedrock":
        return _make_bedrock(settings, providers_cfg)
    raise RuntimeError(
        f"Unknown INQTRIX_LLM_PROVIDER={selector!r}; "
        "expected one of litellm, anthropic, azure, bedrock."
    )


def _build_search_provider(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "SearchProvider":
    """Dispatch the search axis selector to the matching ``_make_*`` builder."""
    selector = _sel(providers_cfg, "search_provider", "perplexity")
    if selector == "perplexity":
        return _make_perplexity(settings, providers_cfg)
    if selector == "azure_foundry":
        return _make_azure_foundry(settings, providers_cfg)
    if selector == "brave":
        log.warning(
            "CONFIG: INQTRIX_SEARCH_PROVIDER=brave is reserved but not "
            "implemented in this build."
        )
        raise RuntimeError(
            "INQTRIX_SEARCH_PROVIDER=brave is reserved but not implemented in "
            "this build; use perplexity or azure_foundry."
        )
    raise RuntimeError(
        f"Unknown INQTRIX_SEARCH_PROVIDER={selector!r}; "
        "expected one of perplexity, azure_foundry."
    )


def _make_litellm(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "LLMProvider":
    """Build the LiteLLM provider (default axis; historical behaviour)."""
    from inqtrix.providers.litellm import LiteLLM

    kwargs = _common_llm_model_kwargs(settings, providers_cfg)
    kwargs.update(
        api_key=settings.server.litellm_api_key,
        base_url=settings.server.litellm_base_url,
    )
    token_budget = _sel(providers_cfg, "token_budget_parameter", "")
    if token_budget:
        kwargs["token_budget_parameter"] = token_budget
    _warn_ignored(_sel(providers_cfg, "temperature", None), "INQTRIX_TEMPERATURE", "litellm")

    llm = LiteLLM(**kwargs)
    llm._client = OpenAI(
        base_url=settings.server.litellm_base_url,
        api_key=settings.server.litellm_api_key,
        max_retries=0,
    )
    return llm


def _make_anthropic(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "LLMProvider":
    """Build the direct Anthropic provider; requires ``ANTHROPIC_API_KEY``."""
    from inqtrix.providers.anthropic import AnthropicLLM

    kwargs = _common_llm_model_kwargs(settings, providers_cfg)
    kwargs["api_key"] = _require(
        _sel(providers_cfg, "anthropic_api_key", ""), "ANTHROPIC_API_KEY", "anthropic"
    )
    base_url = _sel(providers_cfg, "anthropic_base_url", "")
    if base_url:
        kwargs["base_url"] = base_url
    temperature = _sel(providers_cfg, "temperature", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    _warn_ignored(
        _sel(providers_cfg, "token_budget_parameter", ""),
        "INQTRIX_TOKEN_BUDGET_PARAMETER",
        "anthropic",
    )
    return AnthropicLLM(**kwargs)


def _make_azure_openai(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "LLMProvider":
    """Build the Azure OpenAI provider (key-auth or Service-Principal)."""
    from inqtrix.providers.azure import AzureOpenAILLM

    kwargs = _common_llm_model_kwargs(settings, providers_cfg)
    kwargs["azure_endpoint"] = _require(
        _sel(providers_cfg, "azure_openai_endpoint", ""),
        "AZURE_OPENAI_ENDPOINT",
        "azure",
    )
    api_key = _sel(providers_cfg, "azure_openai_api_key", "")
    tenant_id = _sel(providers_cfg, "azure_tenant_id", "")
    client_id = _sel(providers_cfg, "azure_client_id", "")
    client_secret = _sel(providers_cfg, "azure_client_secret", "")
    if api_key:
        kwargs["api_key"] = api_key
    elif tenant_id and client_id and client_secret:
        kwargs.update(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
    else:
        log.warning(
            "CONFIG: INQTRIX_LLM_PROVIDER=azure needs AZURE_OPENAI_API_KEY or the "
            "complete Service-Principal trio (AZURE_TENANT_ID/AZURE_CLIENT_ID/"
            "AZURE_CLIENT_SECRET); failing loudly."
        )
        raise RuntimeError(
            "Selected provider 'azure' requires AZURE_OPENAI_API_KEY or the "
            "complete AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET trio."
        )
    temperature = _sel(providers_cfg, "temperature", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    token_budget = _sel(providers_cfg, "token_budget_parameter", "")
    if token_budget:
        kwargs["token_budget_parameter"] = token_budget
    return AzureOpenAILLM(**kwargs)


def _make_bedrock(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "LLMProvider":
    """Build the AWS Bedrock provider (boto3 credential chain)."""
    from inqtrix.providers.bedrock import BedrockLLM

    kwargs = _common_llm_model_kwargs(settings, providers_cfg)
    kwargs.update(
        profile_name=_sel(providers_cfg, "aws_profile", "") or None,
        region_name=_sel(providers_cfg, "aws_region", "eu-central-1"),
        timeout=settings.agent.reasoning_timeout,
    )
    temperature = _sel(providers_cfg, "temperature", None)
    if temperature is not None:
        kwargs["temperature"] = temperature
    _warn_ignored(
        _sel(providers_cfg, "token_budget_parameter", ""),
        "INQTRIX_TOKEN_BUDGET_PARAMETER",
        "bedrock",
    )
    return BedrockLLM(**kwargs)


def _make_perplexity(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "SearchProvider":
    """Build the Perplexity search provider (default axis).

    Kept lenient on the key to preserve the historical default path
    byte-for-byte (an empty key fails on the first search call, as today),
    while ``search_preset``/``search_instructions`` become env-reachable.
    """
    from inqtrix.providers.perplexity import PerplexitySearch

    kwargs: dict[str, Any] = {
        "api_key": settings.server.perplexity_api_key,
        "base_url": settings.server.perplexity_base_url or None,
        "model": settings.models.search_model or None,
        "cache_maxsize": settings.agent.search_cache_maxsize,
        "cache_ttl": settings.agent.search_cache_ttl,
        "timeout": settings.agent.search_timeout,
    }
    preset = _sel(providers_cfg, "search_preset", "")
    if preset:
        kwargs["preset"] = preset
    instructions = _sel(providers_cfg, "search_instructions", "")
    if instructions:
        kwargs["instructions"] = instructions
    return PerplexitySearch(**kwargs)


def _make_azure_foundry(
    settings: "Settings", providers_cfg: "ProviderSettings | None"
) -> "SearchProvider":
    """Build the Azure AI Foundry web-search provider (key or SP auth)."""
    from inqtrix.providers.azure_web_search import AzureFoundryWebSearch

    kwargs: dict[str, Any] = {
        "project_endpoint": _require(
            _sel(providers_cfg, "azure_ai_project_endpoint", ""),
            "AZURE_AI_PROJECT_ENDPOINT",
            "azure_foundry",
        ),
        "agent_name": _require(
            _sel(providers_cfg, "web_search_agent_name", ""),
            "WEB_SEARCH_AGENT_NAME",
            "azure_foundry",
        ),
        "timeout": settings.agent.search_timeout,
        "max_concurrency": _sel(
            providers_cfg,
            "azure_foundry_max_concurrency",
            _field_default("azure_foundry_max_concurrency"),
        ),
    }
    agent_version = _sel(providers_cfg, "web_search_agent_version", "")
    if agent_version:
        kwargs["agent_version"] = agent_version
    api_key = _sel(providers_cfg, "azure_ai_project_api_key", "")
    tenant_id = _sel(providers_cfg, "azure_tenant_id", "")
    client_id = _sel(providers_cfg, "azure_client_id", "")
    client_secret = _sel(providers_cfg, "azure_client_secret", "")
    if api_key:
        kwargs["api_key"] = api_key
    elif tenant_id and client_id and client_secret:
        kwargs.update(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )
    else:
        log.warning(
            "CONFIG: INQTRIX_SEARCH_PROVIDER=azure_foundry needs "
            "AZURE_AI_PROJECT_API_KEY or the complete Service-Principal trio; "
            "failing loudly."
        )
        raise RuntimeError(
            "Selected provider 'azure_foundry' requires AZURE_AI_PROJECT_API_KEY "
            "or the complete AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET trio."
        )
    _warn_ignored(_sel(providers_cfg, "search_preset", ""), "INQTRIX_SEARCH_PRESET", "azure_foundry")
    _warn_ignored(
        _sel(providers_cfg, "search_instructions", ""),
        "INQTRIX_SEARCH_INSTRUCTIONS",
        "azure_foundry",
    )
    return AzureFoundryWebSearch(**kwargs)
