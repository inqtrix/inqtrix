"""Azure OpenAI adapter for the LLMProvider interface.

This module calls the Azure OpenAI v1 Chat Completions API via the
official ``openai`` SDK's generic ``OpenAI`` client.  SDK retries are
disabled and the provider owns a small visible retry loop so transient
attempts can be logged and streamed to the UI.

Three authentication modes are supported:

* **API key** — simplest path; pass ``api_key`` to the constructor.
* **Entra ID (Service Principal)** — pass ``tenant_id``, ``client_id``
  and ``client_secret`` directly to the constructor; the provider
  builds a ``ClientSecretCredential`` and a token provider internally.
  Alternatively pass a pre-built ``credential`` object.
* **Custom token provider** — pass an ``azure_ad_token_provider``
  callable obtained from ``azure.identity.get_bearer_token_provider``.
  Use this for Managed Identity, AzureCliCredential, or any other
  credential type. Requires the ``azure-identity`` package::

      uv sync

Enterprise environments that route traffic through an HTTP proxy can
set ``proxy_url`` — the provider will create an ``httpx`` client with
that proxy and inject it into the client constructor.

Key differences from the ``LiteLLM`` provider:

* Uses the Azure OpenAI **v1** endpoint format with
    ``OpenAI(base_url="https://.../openai/v1/")``.  This avoids the old
    date-based ``api_version`` churn and follows Microsoft's current
    guidance for new integrations.

* The ``model`` parameter in ``chat.completions.create()`` is the
  **deployment name**, not the model name.  This is an Azure-specific
  convention documented in the example script.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Literal

from openai import DefaultHttpxClient, OpenAI, OpenAIError, RateLimitError, APIStatusError

from inqtrix.constants import (
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT,
)
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRateLimited,
    AzureOpenAIAPIError,
)
from inqtrix.providers._azure_common import (
    AZURE_OPENAI_DEFAULT_SCOPE,
    build_azure_openai_token_provider,
    extract_azure_api_error_details,
    normalize_openai_v1_base_url,
)
from inqtrix.providers._schema import strictify_json_schema
from inqtrix.providers.base import (
    ChatTurn,
    LLMProvider,
    LLMResponse,
    REASONING_EFFORT_LEVELS,
    StructuredLLMResponse,
    _NonFatalNoticeMixin,
    _RetryNoticeMixin,
    _bounded_timeout,
    _call_openai_chat_completion_with_retries,
    _check_deadline,
    _operation_deadline,
    chat_turn_from_openai_response,
    model_capacity_reason,
    _normalize_completion_response,
    normalize_reasoning_effort,
    parse_structured_response_content,
    _sdk_error_code,
    validate_reasoning_effort,
)
from inqtrix.settings import ModelSettings
from inqtrix.state import track_tokens

log = logging.getLogger("inqtrix")


# ---------------------------------------------------------------------------
# Reasoning-effort capability surface
#
# Per Microsoft Foundry doc (last updated 2026-04-30,
# https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
# the full enum for ``reasoning_effort`` is
# {none, minimal, low, medium, high, xhigh}. Per-value-per-model
# restrictions (xhigh requires gpt-5.1-codex-max, none requires gpt-5.1+,
# minimal requires the original GPT-5, etc.) are NOT validated client-side
# because the matrix has too many axes (model x version x value x deployment
# date) and the deployment name does not reliably encode the model family.
# Azure returns HTTP 400 with a clear message when a value is rejected.
# ---------------------------------------------------------------------------
_AZURE_EFFORT_VALUES: frozenset[str] = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh"}
)

# Substring fragments that strongly suggest a NON-reasoning Azure deployment.
# Used only for soft-warnings; never blocks a request. Azure-deployments are
# user-named, so a positive match is high-signal but a non-match never proves
# reasoning capability. Per the Foundry doc o1-mini does not support
# ``reasoning_effort`` at all so it lives in this list.
_AZURE_EFFORT_KNOWN_NONREASONING: tuple[str, ...] = (
    # GPT-4o family — both common naming styles ("gpt-4o" with dash and
    # "gpt4o" without) appear in Azure deployments.
    "gpt-4o",
    "gpt4o",
    "gpt-4-turbo",
    "gpt4-turbo",
    "gpt4turbo",
    "gpt-4-0",
    "gpt4-0",
    "gpt-4-1",
    "gpt4-1",
    "gpt-3.5",
    "gpt3.5",
    "gpt-35",
    "gpt35",
    # Legacy completion models.
    "davinci",
    "babbage",
    "ada",
    "curie",
    # Non-text endpoints — they would never accept reasoning_effort.
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    # o1-mini does not accept reasoning_effort at all per Foundry doc.
    "o1-mini",
)
_AZURE_STRUCTURED_OUTPUT_UNSUPPORTED_FRAGMENTS: tuple[str, ...] = (
    "embedding",
    "whisper",
    "tts",
    "dall-e",
    "gpt-3.5",
    "gpt3.5",
    "gpt-35",
    "gpt35",
)


def _deployment_may_support_structured_output(model: str) -> bool:
    """Return False only for deployment names that clearly cannot emit JSON schemas."""
    if not model:
        return False
    lowered = model.lower()
    return not any(
        fragment in lowered
        for fragment in _AZURE_STRUCTURED_OUTPUT_UNSUPPORTED_FRAGMENTS
    )


class AzureOpenAILLM(_RetryNoticeMixin, _NonFatalNoticeMixin, LLMProvider):
    """Call the Azure OpenAI v1 Chat Completions API via the official SDK.

    Use this provider when your reasoning models are deployed on Azure
    OpenAI and you want the current v1 endpoint shape instead of the
    legacy date-based API-version flow. It is a good fit for Azure-native
    deployments that still want the same OpenAI SDK ergonomics as
    LiteLLM-backed providers.

    Attributes:
        _default_model (str): Primary deployment name for reasoning calls.
        _claim_extract_model (str): Deployment used for claim extraction.
        _default_max_tokens (int): Output-token budget for reasoning
            requests.
        _temperature (float | None): Optional sampling temperature.
        _token_budget_parameter (Literal["max_completion_tokens", "max_tokens"]):
            Request field used for output-token budgeting.
        _request_params (dict[str, Any]): Extra request parameters merged
            into reasoning calls.
        _models (ModelSettings): Effective role-to-model mapping exposed
            to the runtime.
        _client (OpenAI): Shared SDK client for Azure OpenAI requests.
    """

    complete_uses_metadata: bool = True

    def __init__(
        self,
        *,
        azure_endpoint: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        azure_ad_token_provider: Callable[[], str] | None = None,
        credential: Any | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_scope: str = AZURE_OPENAI_DEFAULT_SCOPE,
        default_model: str = "gpt-4o",
        classify_model: str = "",
        claim_extract_model: str = "",
        evaluate_model: str = "",
        plan_model: str = "",
        answer_model: str = "",
        direct_chat_model: str = "",
        tier_high_model: str = "",
        tier_mid_model: str = "",
        tier_fast_model: str = "",
        tier_high_effort: str = "",
        tier_mid_effort: str = "",
        tier_fast_effort: str = "",
        default_max_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        context_window_tokens: int | None = None,
        temperature: float | None = None,
        token_budget_parameter: Literal["max_completion_tokens",
                                        "max_tokens"] = "max_completion_tokens",
        proxy_url: str | None = None,
        timeout: float = REASONING_TIMEOUT,
        default_headers: Mapping[str, str] | None = None,
        request_params: Mapping[str, Any] | None = None,
        default_reasoning_effort: str | None = None,
        selectable_models: list[str] | None = None,
    ) -> None:
        """Initialize the Azure OpenAI provider.

        Use the constructor when the reasoning path should run against an
        Azure OpenAI deployment name rather than a raw model name. Exactly
        one endpoint input and exactly one authentication path must be
        supplied. Role-specific deployments and extra request parameters
        let you optimize cost or compatibility without changing graph code.

        Args:
            azure_endpoint: Azure OpenAI resource endpoint such as
                ``"https://my-resource.openai.azure.com/"``. When this is
                provided, ``base_url`` must be omitted.
            base_url: Full Azure OpenAI v1 base URL such as
                ``"https://my-resource.openai.azure.com/openai/v1/"``.
                When this is provided, ``azure_endpoint`` must be omitted.
            api_key: Azure OpenAI API key. Do not use this together with
                ``azure_ad_token_provider`` or any of the credential
                arguments.
            azure_ad_token_provider: Bearer-token provider from
                ``azure.identity.get_bearer_token_provider(...)``. Use
                this for Managed Identity, AzureCliCredential, or any
                custom credential type. Mutually exclusive with
                ``api_key``, ``credential`` and the Service Principal
                fields.
            credential: Optional pre-built ``azure.identity`` credential
                object (any ``TokenCredential``-like instance). When
                supplied the provider builds an internal bearer-token
                provider from it. Mutually exclusive with ``api_key``,
                ``azure_ad_token_provider`` and the Service Principal
                fields.
            tenant_id: Entra tenant ID for automatic Service Principal
                auth. Must be supplied together with ``client_id`` and
                ``client_secret``. Mutually exclusive with ``api_key``,
                ``azure_ad_token_provider`` and ``credential``.
            client_id: Entra client ID for automatic Service Principal
                auth. See ``tenant_id``.
            client_secret: Entra client secret for automatic Service
                Principal auth. See ``tenant_id``.
            token_scope: OAuth scope used when building an internal
                token provider from ``credential`` or the Service
                Principal fields. Defaults to the Azure OpenAI scope
                (``https://cognitiveservices.azure.com/.default``).
                Override only when targeting a non-OpenAI Azure surface.
            default_model: Primary deployment name for classify, plan,
                evaluate fallback, and answer calls. This must be the Azure
                deployment name, not the base model name.
            classify_model: Optional deployment override for question
                classification. When omitted, classification falls back to
                ``default_model``.
            claim_extract_model: Optional cheaper deployment for claim
                extraction. When omitted, claim extraction uses
                ``default_model``.
            evaluate_model: Optional deployment override for evidence
                evaluation. When omitted, evaluation falls back to
                ``default_model``.
            plan_model: Optional per-node deployment override for the plan node.
            answer_model: Optional per-node deployment override for the answer
                node.
            direct_chat_model: Optional per-node deployment override for the
                skip-search direct-chat node.
            tier_high_model: Deployment for the high tier (answer by default).
            tier_mid_model: Deployment for the mid tier
                (plan/evaluate/direct_chat).
            tier_fast_model: Deployment for the fast tier
                (classify/claim_extract). Nodes map to a tier via
                ``inqtrix.model_routing.NODE_TIER_ASSIGNMENT``; empty tiers and
                per-node overrides both fall back to ``default_model``.
            tier_high_effort: Per-tier reasoning effort for the high tier.
            tier_mid_effort: Per-tier reasoning effort for the mid tier.
            tier_fast_effort: Per-tier reasoning effort for the fast tier.
                ``""`` inherits ``default_reasoning_effort``, ``"none"`` forces
                reasoning off, and a graded level (``minimal``..``xhigh``)
                enables it.
            default_max_tokens: Output-token budget for reasoning calls.
                The default is intentionally high to avoid hidden
                truncation by small provider defaults.
            context_window_tokens: Known context-window size for the
                Azure deployment. ``None`` means unknown capacity.
            temperature: Optional sampling temperature. The default is
                ``None``.
            token_budget_parameter: Which request field to use for output
                budgets. Keep the default ``"max_completion_tokens"`` for
                newer deployments and use ``"max_tokens"`` only when a
                specific deployment still requires the older field.
            proxy_url: Optional HTTPS proxy URL. When omitted, the default
                HTTP transport is used.
            timeout: Default client-level timeout in seconds. The default
                is ``60.0``.
            default_headers: Optional headers forwarded on every request,
                for example preview feature headers.
            request_params: Optional extra parameters merged into reasoning
                calls after reserved SDK keys are filtered out.
            default_reasoning_effort: Optional reasoning effort applied to
                classify, plan, evaluate, and answer calls. Forwarded to
                the Chat Completions API as the top-level
                ``reasoning_effort`` field. Accepted values are
                ``"none"``, ``"minimal"``, ``"low"``, ``"medium"``,
                ``"high"``, and ``"xhigh"``. The provider validates the
                set; per-value-per-model compatibility is enforced by
                Azure (it returns HTTP 400 with a clear message on
                mismatch). When ``None`` (default) the field is not
                sent. Setting an explicit value while
                ``request_params`` already carries a different
                ``reasoning_effort`` triggers a soft warning that the
                explicit constructor argument wins. Mutually exclusive
                with ``temperature`` because Azure reasoning models
                reject ``temperature``.
        Note:
            Per-model caveats (Microsoft Foundry doc, last updated
            2026-04-30): ``"none"`` requires ``gpt-5.1+``;
            ``"minimal"`` requires the original GPT-5 (not
            ``gpt-5.1+``, not ``gpt-5-codex``); ``"xhigh"`` requires
            ``gpt-5.1-codex-max``; ``gpt-5-pro`` only accepts
            ``"high"``; ``o1-mini`` does not support
            ``reasoning_effort`` at all; ``gpt-5.1+`` defaults to
            ``"none"`` so explicit ``default_reasoning_effort="medium"``
            (or higher) is required after upgrading from gpt-5 if
            reasoning should still occur.

        Raises:
            ValueError: If neither or both of ``azure_endpoint`` and
                ``base_url`` are provided, if more than one of the auth
                modes (``api_key``, ``azure_ad_token_provider``,
                ``credential``, Service Principal fields) is supplied,
                if none is supplied, if the Service Principal fields
                are partially supplied, if ``token_budget_parameter``
                is invalid, if ``default_reasoning_effort`` is not in
                the accepted value set, or if ``temperature`` is set
                together with ``default_reasoning_effort``.

        Example:
            >>> from inqtrix import AzureOpenAILLM
            >>> llm = AzureOpenAILLM(
            ...     azure_endpoint="https://example.openai.azure.com/",
            ...     api_key="test-key",
            ...     default_model="my-gpt4o-deployment",
            ... )
            >>> llm.models.reasoning_model
            'my-gpt4o-deployment'
        """
        if bool(azure_endpoint) == bool(base_url):
            raise ValueError(
                "Provide exactly one of azure_endpoint or base_url."
            )

        sp_fields = (tenant_id, client_id, client_secret)
        sp_any = any(sp_fields)
        sp_all = all(sp_fields)
        if sp_any and not sp_all:
            raise ValueError(
                "tenant_id, client_id and client_secret must all be "
                "provided together for Service Principal auth."
            )

        auth_modes_chosen = sum(
            1
            for present in (
                bool(api_key),
                bool(azure_ad_token_provider),
                bool(credential),
                sp_all,
            )
            if present
        )
        if auth_modes_chosen > 1:
            raise ValueError(
                "api_key, azure_ad_token_provider, credential and the "
                "Service Principal fields (tenant_id/client_id/client_secret) "
                "are mutually exclusive — pass exactly one auth mode."
            )
        if auth_modes_chosen == 0:
            raise ValueError(
                "An auth mode must be provided: api_key, azure_ad_token_provider, "
                "credential, or tenant_id+client_id+client_secret."
            )

        if azure_ad_token_provider is None and (credential is not None or sp_all):
            azure_ad_token_provider = build_azure_openai_token_provider(
                credential=credential,
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret,
                scope=token_scope,
            )

        if token_budget_parameter not in {"max_completion_tokens", "max_tokens"}:
            raise ValueError(
                "token_budget_parameter must be 'max_completion_tokens' or 'max_tokens'."
            )

        if default_reasoning_effort is not None and default_reasoning_effort not in _AZURE_EFFORT_VALUES:
            raise ValueError(
                f"default_reasoning_effort must be one of {sorted(_AZURE_EFFORT_VALUES)}."
            )

        if temperature is not None and default_reasoning_effort is not None:
            raise ValueError(
                "temperature and default_reasoning_effort are mutually exclusive — "
                "Azure reasoning models reject requests that set temperature. "
                "Either drop temperature or drop default_reasoning_effort."
            )

        self._default_model = default_model
        self._claim_extract_model = claim_extract_model or default_model
        self._default_max_tokens = default_max_tokens
        self._context_window_tokens = context_window_tokens
        self._selectable_models = list(selectable_models or [])
        self._temperature = temperature
        self._token_budget_parameter = token_budget_parameter
        self._request_params = dict(request_params or {})

        self._effort_config_warnings: list[str] = []
        if default_reasoning_effort is not None:
            existing = self._request_params.get("reasoning_effort")
            if existing is not None and existing != default_reasoning_effort:
                self._effort_config_warnings.append(
                    f"AzureOpenAILLM: request_params['reasoning_effort']='{existing}' "
                    f"is overridden by constructor argument "
                    f"'{default_reasoning_effort}'. Remove one of the two values "
                    "to resolve this mismatch."
                )
            self._request_params["reasoning_effort"] = default_reasoning_effort

        self._effort_config_warnings.extend(
            self._collect_effort_warnings(
                default_model=default_model,
                classify_model=classify_model,
                claim_extract_model=self._claim_extract_model,
                evaluate_model=evaluate_model,
                default_effort=default_reasoning_effort,
            )
        )
        for warning in self._effort_config_warnings:
            log.warning(warning)
        self._models = ModelSettings(
            reasoning_model=default_model,
            search_model="",
            classify_model=classify_model,
            claim_extract_model=claim_extract_model,
            evaluate_model=evaluate_model,
            plan_model=plan_model,
            answer_model=answer_model,
            direct_chat_model=direct_chat_model,
            tier_high_model=tier_high_model,
            tier_mid_model=tier_mid_model,
            tier_fast_model=tier_fast_model,
            tier_high_effort=tier_high_effort,
            tier_mid_effort=tier_mid_effort,
            tier_fast_effort=tier_fast_effort,
        )

        # Build optional httpx client for proxy support.
        http_client = None
        if proxy_url:
            import httpx

            http_client = DefaultHttpxClient(
                proxy=proxy_url,
                timeout=httpx.Timeout(timeout, connect=10.0),
            )

        auth_value: str | Callable[[], str] = azure_ad_token_provider or api_key or ""

        client_kwargs: dict[str, Any] = {
            "base_url": self._normalize_base_url(base_url or azure_endpoint or ""),
            "api_key": auth_value,
            "timeout": timeout,
            "max_retries": 0,
        }
        if default_headers:
            client_kwargs["default_headers"] = dict(default_headers)
        if http_client is not None:
            client_kwargs["http_client"] = http_client

        self._client = OpenAI(**client_kwargs)

    _normalize_base_url = staticmethod(normalize_openai_v1_base_url)
    _extract_api_error_details = staticmethod(extract_azure_api_error_details)

    def _merge_request_params(
        self,
        base_kwargs: dict[str, Any],
        extra_params: Mapping[str, Any],
    ) -> dict[str, Any]:
        reserved = {
            "model",
            "messages",
            "timeout",
            "stream",
            "max_tokens",
            "max_completion_tokens",
        }
        merged = dict(base_kwargs)
        for key, value in extra_params.items():
            if key in reserved:
                continue
            merged[key] = value
        return merged

    def _apply_call_reasoning_effort(
        self,
        create_kwargs: dict[str, Any],
        reasoning_effort: str | None,
        *,
        use_model: str,
    ) -> dict[str, Any]:
        """Apply a per-call ``reasoning_effort`` override to request kwargs.

        ``None``/``""`` leaves the kwargs untouched, so the constructor
        ``default_reasoning_effort`` (already merged via ``self._request_params``)
        still applies. ``"none"`` (or an unsupported level, which is downgraded
        to ``"none"`` with a visible warning) forces reasoning off by *omitting*
        ``reasoning_effort`` entirely -- this is 400-safe for non-reasoning
        deployments (e.g. gpt-4o) and matches the gpt-5.x default of no implicit
        reasoning. A graded level is sent as ``reasoning_effort`` and, because
        Azure rejects it together with ``temperature``, drops ``temperature``
        for this call. A graded level on an incompatible deployment surfaces
        loudly as an Azure HTTP 400.

        Args:
            create_kwargs: The request kwargs built so far (post-merge).
            reasoning_effort: The per-call effort override.
            use_model: The effective deployment, used for warning context.

        Returns:
            The (possibly copied) request kwargs with the effort applied.
        """
        token = normalize_reasoning_effort(reasoning_effort)
        if token == "":
            return create_kwargs
        out = dict(create_kwargs)
        if token == "none":
            out.pop("reasoning_effort", None)
            return out
        effort, warnings = validate_reasoning_effort(
            token,
            supported_levels=REASONING_EFFORT_LEVELS,
            label=f"AzureOpenAILLM({use_model})",
        )
        for warning in warnings:
            log.warning("CONFIG: %s", warning)
        if effort == "none":
            out.pop("reasoning_effort", None)
            return out
        out["reasoning_effort"] = effort
        out.pop("temperature", None)
        return out

    @staticmethod
    def _looks_like_nonreasoning_deployment(model: str) -> bool:
        """Return True when the deployment name strongly suggests a
        non-reasoning Azure OpenAI model (gpt-4o, gpt-3.5, embeddings,
        and o1-mini which does not accept ``reasoning_effort`` at all).

        Used only for soft-warnings emitted at construction time. Azure
        is the authoritative compatibility source — a positive match
        here is high-signal but a non-match never proves reasoning
        capability, because deployment names are user-chosen.
        """
        if not model:
            return False
        lowered = model.lower()
        return any(
            frag in lowered for frag in _AZURE_EFFORT_KNOWN_NONREASONING
        )

    def _collect_effort_warnings(
        self,
        *,
        default_model: str,
        classify_model: str,
        claim_extract_model: str,
        evaluate_model: str,
        default_effort: str | None,
    ) -> list[str]:
        """Return soft-warnings for known-non-reasoning deployments.

        Conservative: only warns when the deployment name contains a
        fragment from :data:`_AZURE_EFFORT_KNOWN_NONREASONING`.
        Unfamiliar names pass through silently and Azure decides on the
        first call whether the deployment accepts the value.
        """
        warnings: list[str] = []
        pairs: list[tuple[str, str, str | None]] = [
            ("default_model", default_model, default_effort),
            ("classify_model", classify_model, default_effort),
            ("evaluate_model", evaluate_model, default_effort),
            ("claim_extract_model", claim_extract_model, default_effort),
        ]
        for role, model, effort in pairs:
            if effort is None or not model:
                continue
            if not self._looks_like_nonreasoning_deployment(model):
                continue
            warnings.append(
                f"AzureOpenAILLM: reasoning_effort='{effort}' is set for "
                f"{role}='{model}', but the deployment name contains a marker "
                "for a non-reasoning model. Azure is likely to reject the "
                "request with HTTP 400."
            )
        return warnings

    def consume_effort_config_warnings(self) -> list[str]:
        """Return and clear constructor-time effort/model warnings.

        Drained by the classify node on the first run so warnings
        appear in both the progress feed and the inqtrix log line. The
        method matches the duck-typed contract used by ``classify``
        (also implemented by ``AnthropicLLM`` and
        ``BedrockLLM``).
        """
        out = list(self._effort_config_warnings)
        self._effort_config_warnings = []
        return out

    # -- model metadata ----------------------------------------------------

    @property
    def models(self) -> ModelSettings:
        """Return the effective role-to-model mapping for the runtime.

        Returns:
            ModelSettings: Resolved deployment names used by graph nodes.
        """
        return self._models

    @property
    def selectable_models(self) -> list[str]:
        """Return the operator-curated model ids offered for direct selection."""
        return self._selectable_models

    @property
    def context_window_tokens(self) -> int | None:
        """Return the configured context window for capacity checks."""
        return self._context_window_tokens

    def _create_chat_completion_with_retry(
        self,
        *,
        create_kwargs: dict[str, Any],
        model: str,
        operation: str,
        timeout: float,
        deadline: float | None,
    ) -> Any:
        """Call Chat Completions with visible transient-error retries."""
        self._clear_retry_notices()

        def _error_code(exc: BaseException) -> str:
            if isinstance(exc, APIStatusError):
                details = self._extract_api_error_details(exc)
                return str(details.get("error_code") or _sdk_error_code(exc))
            return _sdk_error_code(exc)

        def _request_id(exc: BaseException) -> str:
            if isinstance(exc, APIStatusError):
                details = self._extract_api_error_details(exc)
                return str(details.get("request_id") or "")
            return ""

        operation_deadline = _operation_deadline(timeout, deadline)
        return _call_openai_chat_completion_with_retries(
            provider_label="AzureOpenAI",
            model=model,
            operation=operation,
            deadline=operation_deadline,
            outer_deadline=deadline,
            timeout_label=f'Azure OpenAI call ({operation})',
            configured_timeout_seconds=timeout,
            create=lambda: self._client.chat.completions.create(
                **{
                    **create_kwargs,
                    "timeout": _bounded_timeout(timeout, operation_deadline),
                }
            ),
            append_retry_notice=self._append_retry_notice,
            error_code_for=_error_code,
            request_id_for=_request_id,
        )

    # -- LLMProvider interface ---------------------------------------------

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Generate text through Azure OpenAI and discard token metadata.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction.
            model: Optional deployment override. When omitted, the
                provider uses ``self._default_model``.
            max_output_tokens: Output-token budget for this call. When
                ``None`` (default), falls back to
                ``self._default_max_tokens`` (constructor argument
                ``default_max_tokens``). The value is forwarded under
                the configured ``token_budget_parameter`` key.
            timeout: Per-call timeout budget in seconds.
            state: Optional mutable agent state for token tracking.
            deadline: Optional absolute monotonic deadline for the full
                run.

        Returns:
            str: Visible assistant text for the completion.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Azure returns a fatal rate-limit error.
            AzureOpenAIAPIError: If the SDK reports a non-rate-limit
                backend failure.
        """
        return self.complete_with_metadata(
            prompt,
            system=system,
            model=model,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            state=state,
            deadline=deadline,
            reasoning_effort=reasoning_effort,
        ).content

    def complete_with_metadata(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Generate text and metadata through Azure OpenAI.

        Use this method for reasoning calls when the runtime wants token
        accounting in addition to visible content. The method clamps the
        timeout against the remaining deadline, injects the configured
        token-budget field, merges optional request parameters, and uses
        the provider's visible retry loop for transient failures.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction. The default is ``None``.
            model: Optional deployment override. When omitted, the
                provider uses ``self._default_model``.
            max_output_tokens: Output-token budget for this call. When
                ``None`` (default), falls back to
                ``self._default_max_tokens``. Forwarded under the
                configured ``token_budget_parameter`` key, so newer
                Azure deployments receive ``max_completion_tokens``
                while legacy deployments receive ``max_tokens``.
            timeout: Per-call timeout budget in seconds before deadline
                clamping. The default is ``REASONING_TIMEOUT``.
            state: Optional mutable agent state that receives token counts
                through ``track_tokens()`` when provided.
            deadline: Optional absolute monotonic deadline for the full
                run.

        Returns:
            LLMResponse: Structured response containing visible content,
            token counts, and the effective deployment label.

        Raises:
            AgentTimeout: If the full run deadline has already elapsed.
            AgentRateLimited: If Azure returns HTTP 429 or the SDK raises
                ``RateLimitError``.
            AzureOpenAIAPIError: If Azure responds with a non-rate-limit
                API error or another SDK-level failure occurs.
        """
        if deadline is not None:
            _check_deadline(deadline)

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        use_model = model or self._default_model

        create_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "timeout": _bounded_timeout(timeout, deadline),
            "stream": False,
        }
        create_kwargs[self._token_budget_parameter] = max_output_tokens or self._default_max_tokens
        if self._temperature is not None:
            create_kwargs["temperature"] = self._temperature
        create_kwargs = self._merge_request_params(create_kwargs, self._request_params)
        create_kwargs = self._apply_call_reasoning_effort(
            create_kwargs, reasoning_effort, use_model=use_model
        )

        try:
            r = self._create_chat_completion_with_retry(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="complete",
                timeout=timeout,
                deadline=deadline,
            )
            normalized = _normalize_completion_response(r)
            if state is not None:
                track_tokens(state, normalized)
            return LLMResponse(
                content=normalized.content,
                prompt_tokens=normalized.prompt_tokens,
                completion_tokens=normalized.completion_tokens,
                cache_read_tokens=normalized.cache_read_tokens,
                cache_creation_tokens=normalized.cache_creation_tokens,
                reasoning_tokens=normalized.reasoning_tokens,
                model=use_model,
                finish_reason=normalized.finish_reason,
                raw=normalized.raw,
                request_max_tokens=int(
                    create_kwargs.get(self._token_budget_parameter) or 0
                ),
            )
        except RateLimitError as e:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AgentRateLimited(use_model, e)
        except APIStatusError as e:
            if e.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentRateLimited(use_model, e)
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            details = self._extract_api_error_details(e)
            log.error(
                "Azure-OpenAI call failed (model=%s, status=%s, code=%s, request-id=%s,"
                " error_type=%s)",
                use_model,
                details.get("status_code"),
                details.get("error_code") or "-",
                details.get("request_id") or "-",
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                status_code=details.get("status_code") if isinstance(
                    details.get("status_code"), int) else None,
                error_code=str(details.get("error_code") or "").strip(),
                request_id=str(details.get("request_id") or "").strip() or None,
                message=str(details.get("message") or "").strip() or str(e),
                original=e,
            ) from e
        except OpenAIError as e:
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "Azure-OpenAI call failed (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                message=str(e),
                original=e,
            ) from e

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Native function calling is a core Azure OpenAI chat feature
        across current deployments, so the provider opts in
        unconditionally; a deployment without tool support surfaces its
        own API error on the first :meth:`chat` call (visible, never
        silently degraded)."""
        return True

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | None = None,
    ) -> ChatTurn:
        """Run one native tool-calling chat turn.

        Same transport, retry loop, request-parameter merging,
        reasoning-effort mapping, and error mapping as
        :meth:`complete_with_metadata` — the only differences are the
        caller-owned message array and the OpenAI ``tools`` and
        ``tool_choice`` parameters.
        Response parsing goes through the shared
        :func:`chat_turn_from_openai_response` so tool-call rules cannot
        drift between OpenAI-compatible providers.
        """
        if deadline is not None:
            _check_deadline(deadline)
        use_model = model or self._default_model
        create_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": list(messages),
            "timeout": _bounded_timeout(timeout, deadline),
            "stream": False,
        }
        create_kwargs[self._token_budget_parameter] = (
            max_output_tokens or self._default_max_tokens
        )
        if self._temperature is not None:
            create_kwargs["temperature"] = self._temperature
        create_kwargs = self._merge_request_params(
            create_kwargs, self._request_params
        )
        create_kwargs = self._apply_call_reasoning_effort(
            create_kwargs, reasoning_effort, use_model=use_model
        )
        if tools:
            create_kwargs["tools"] = list(tools)
        if tool_choice is not None:
            create_kwargs["tool_choice"] = tool_choice
        try:
            r = self._create_chat_completion_with_retry(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="chat",
                timeout=timeout,
                deadline=deadline,
            )
            return chat_turn_from_openai_response(r, model=use_model)
        except RateLimitError as e:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AgentRateLimited(use_model, e)
        except APIStatusError as e:
            if e.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentRateLimited(use_model, e)
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_chat",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            details = self._extract_api_error_details(e)
            log.error(
                "Azure-OpenAI chat failed (model=%s, status=%s, code=%s, request-id=%s,"
                " error_type=%s)",
                use_model,
                details.get("status_code"),
                details.get("error_code") or "-",
                details.get("request_id") or "-",
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                status_code=details.get("status_code") if isinstance(
                    details.get("status_code"), int) else None,
                error_code=str(details.get("error_code") or "").strip(),
                request_id=str(details.get("request_id") or "").strip() or None,
                message=str(details.get("message") or "").strip() or str(e),
                original=e,
            ) from e
        except OpenAIError as e:
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_chat",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "Azure-OpenAI chat failed (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                message=str(e),
                original=e,
            ) from e

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Return whether the selected deployment may support JSON schemas.

        Azure deployment names are user-defined, so this method can only
        reject names that clearly refer to non-chat or legacy deployments.
        Azure remains the authoritative source on the first request.

        Args:
            model: Optional deployment override. When omitted, the
                provider's default deployment is checked.

        Returns:
            ``True`` unless the deployment name contains a known marker
            for a structured-output-incompatible endpoint.
        """
        return _deployment_may_support_structured_output(model or self._default_model)

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> StructuredLLMResponse:
        """Generate a JSON-schema-constrained Azure OpenAI response.

        Args:
            prompt: User-facing input text.
            schema: JSON Schema object sent through ``response_format``.
            schema_name: Stable schema name used by Azure/OpenAI.
            schema_description: Optional schema purpose. The Chat
                Completions API does not consume this value directly.
            system: Optional system instruction.
            model: Optional deployment override.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout before deadline clamping.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute monotonic deadline.

        Returns:
            StructuredLLMResponse with parsed top-level JSON object.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Azure returns HTTP 429.
            AzureOpenAIAPIError: If Azure responds with a non-rate-limit
                API error or another SDK-level failure occurs.
            AgentStructuredOutputError: If the visible structured JSON
                cannot be parsed into a dictionary.
        """
        del schema_description
        if deadline is not None:
            _check_deadline(deadline)

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        use_model = model or self._default_model

        create_kwargs: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "timeout": _bounded_timeout(timeout, deadline),
            "stream": False,
        }
        create_kwargs[self._token_budget_parameter] = (
            max_output_tokens or self._default_max_tokens
        )
        if self._temperature is not None:
            create_kwargs["temperature"] = self._temperature
        create_kwargs = self._merge_request_params(create_kwargs, self._request_params)
        create_kwargs = self._apply_call_reasoning_effort(
            create_kwargs, reasoning_effort, use_model=use_model
        )
        create_kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                # Strict outputs require every property in `required`, no
                # `default`, and additionalProperties:false. The generic caller
                # hands a canonical Pydantic schema; the provider owns adapting
                # it to ITS endpoint contract (keyed on strict, not on a model).
                "schema": strictify_json_schema(schema),
            },
        }

        try:
            r = self._create_chat_completion_with_retry(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="structured_response",
                timeout=timeout,
                deadline=deadline,
            )
            normalized = _normalize_completion_response(r)
            response = StructuredLLMResponse(
                parsed=parse_structured_response_content(
                    normalized.content,
                    model=use_model,
                    schema_name=schema_name,
                ),
                content=normalized.content,
                prompt_tokens=normalized.prompt_tokens,
                completion_tokens=normalized.completion_tokens,
                cache_read_tokens=normalized.cache_read_tokens,
                cache_creation_tokens=normalized.cache_creation_tokens,
                reasoning_tokens=normalized.reasoning_tokens,
                model=use_model,
                finish_reason=normalized.finish_reason,
                raw=normalized.raw,
                request_max_tokens=int(
                    create_kwargs.get(self._token_budget_parameter) or 0
                ),
                schema_name=schema_name,
            )
            if state is not None:
                track_tokens(state, response)
            return response
        except RateLimitError as e:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AgentRateLimited(use_model, e)
        except APIStatusError as e:
            if e.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentRateLimited(use_model, e)
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            details = self._extract_api_error_details(e)
            log.error(
                "Azure-OpenAI call failed (model=%s, status=%s, code=%s, request-id=%s,"
                " error_type=%s)",
                use_model,
                details.get("status_code"),
                details.get("error_code") or "-",
                details.get("request_id") or "-",
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                status_code=details.get("status_code") if isinstance(
                    details.get("status_code"), int) else None,
                error_code=str(details.get("error_code") or "").strip(),
                request_id=str(details.get("request_id") or "").strip() or None,
                message=str(details.get("message") or "").strip() or str(e),
                original=e,
            ) from e
        except OpenAIError as e:
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "Azure-OpenAI call failed (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AzureOpenAIAPIError(
                model=use_model,
                message=str(e),
                original=e,
            ) from e

    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Configuration here means: an OpenAI SDK client was successfully
        constructed (endpoint resolved, exactly one auth mode supplied,
        token budget parameter validated). This does not guarantee that
        the Azure deployment is reachable or that credentials are valid
        — those failures only surface on the first ``complete()`` call.

        Returns:
            ``True`` when the internal SDK client was constructed,
            otherwise ``False``. In practice ``False`` is unreachable
            today because constructor argument validation raises
            ``ValueError`` on bad input rather than returning a half-
            initialised provider; the method is kept for forward
            compatibility and ABC contract compliance.
        """
        return self._client is not None
