"""Health and model-discovery payload assembly.

Constructor-first configuration applies throughout: every model name shown to
operators reflects what the provider was actually built with, never
the global ``settings.models`` defaults.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from inqtrix import __display_version__
from inqtrix.core.constants import MODEL_NAME
from inqtrix.legal import ai_disclosure_metadata, legal_metadata
from inqtrix.model_cards import build_models_catalog
from inqtrix.model_routing import (
    describe_chat_model_options,
    describe_node_resolutions,
)
from inqtrix.settings import AgentSettings, Settings

if TYPE_CHECKING:
    from inqtrix.auth.principal import AuthProvider
    from inqtrix.providers.base import ProviderContext

log = logging.getLogger("inqtrix")



def provider_label(provider: object) -> str:
    """Return the public class name of a provider, unwrapping adapter shells.

    Follows the ``_provider`` chain to a fixed point so stacked shells
    (tracing wrapper around ``ConfiguredLLMProvider`` around the real
    backend) still report the backend class, never a wrapper name.
    """
    seen: set[int] = set()
    current = provider
    while id(current) not in seen:
        seen.add(id(current))
        wrapped = getattr(current, "_provider", None)
        if wrapped is None:
            break
        current = wrapped
    return type(current).__name__


def provider_ready(provider: object, *, label: str) -> bool:
    """Probe the provider's ``is_available`` hook without raising."""
    try:
        checker = getattr(provider, "is_available", None)
        return bool(checker()) if callable(checker) else False
    except Exception as exc:  # noqa: BLE001 — health probes must not crash the endpoint
        log.warning(
            "Health check for %s failed (error_type=%s)",
            label,
            type(exc).__name__,
        )
        return False


class HealthService:
    """Build the ``/health`` and ``/v1/models`` payloads.

    Args:
        providers: The default provider bundle whose identity and
            readiness the health payload reports.
        settings: Root settings (report profile, risk threshold, and
            the search-model fallback).
        auth_provider: The active auth provider; drives the
            ``auth_required`` flag and the additive ``auth_mode``
            field.
        stacks: Optional multi-stack registry, used to resolve the
            default stack's agent settings for the payload.
        default_stack: Name of the default stack when *stacks* is set.
    """

    def __init__(
        self,
        *,
        providers: "ProviderContext",
        settings: Settings,
        auth_provider: "AuthProvider",
        stacks: dict[str, Any] | None = None,
        default_stack: str = "",
    ) -> None:
        self._providers = providers
        self._settings = settings
        self._auth_provider = auth_provider
        self._stacks = stacks or {}
        self._default_stack = default_stack

    # -- helpers ----------------------------------------------------------- #

    def _resolve_search_model(self, search_provider: object) -> str:
        """Read the standardized ``search_model`` property off the provider.

        Every search provider in :mod:`inqtrix.providers` overrides
        ``SearchProvider.search_model`` to return its operator-facing
        identifier. The default ABC implementation returns
        ``"<ClassName>(unknown)"`` so a custom subclass that forgets
        the override is loud rather than silently leaking the global
        ``Settings.models.search_model``. Falling back to
        ``settings.models.search_model`` is therefore a defensive last
        resort only when ``getattr`` finds nothing (older third-party
        SearchProvider subclasses that do not expose the standardized field).
        """
        value = getattr(search_provider, "search_model", "")
        if isinstance(value, str) and value:
            return value
        return self._settings.models.search_model

    def _resolve_health_models(
        self,
        llm_provider: object,
        search_provider: object,
        agent_settings: AgentSettings,
    ) -> dict[str, Any]:
        """Return the effective per-role + per-node model names for /health.

        Constructor-first configuration: every model name shown to
        operators must reflect what the provider was *actually* built
        with, not what the global ``settings.models`` block defaults
        to. The ``node_models`` block reports, per call site, the model
        and reasoning effort the graph would actually route to (with
        ``model_source`` / ``effort_source`` provenance) — the same
        resolution used at runtime, so nothing is reported that the
        routing did not decide.
        """
        provider_models = getattr(llm_provider, "models", None)
        requested_tier = (agent_settings.model_tier or "").strip() or None
        node_models = describe_node_resolutions(provider_models, requested_tier)

        def _from_node(node: str) -> str:
            return node_models.get(node, {}).get("model", "")

        return {
            "reasoning_model": (
                getattr(provider_models, "reasoning_model", "")
                if provider_models is not None
                else ""
            ),
            "search_model": self._resolve_search_model(search_provider),
            "classify_model": _from_node("classify"),
            "claim_extract_model": _from_node("claim_extract"),
            "evaluate_model": _from_node("evaluate"),
            "node_models": node_models,
            "chat_model_options": describe_chat_model_options(provider_models),
            "models_catalog": build_models_catalog(
                getattr(llm_provider, "selectable_models", []) or []
            ),
            "context_window_tokens": getattr(
                llm_provider, "context_window_tokens", None
            ),
        }

    def _health_agent_settings(self) -> AgentSettings:
        if self._stacks and self._default_stack in self._stacks:
            stack_bundle = self._stacks[self._default_stack]
            stack_settings = getattr(stack_bundle, "agent_settings", None)
            if stack_settings is not None:
                return stack_settings
        return self._settings.agent

    # -- payloads ----------------------------------------------------------- #

    def health_payload(self) -> tuple[int, dict[str, Any]]:
        """Build the liveness payload and its HTTP status code."""
        llm_label = provider_label(self._providers.llm)
        search_label = provider_label(self._providers.search)
        llm_ready = provider_ready(self._providers.llm, label=llm_label)
        search_ready = provider_ready(self._providers.search, label=search_label)
        status_code = 200 if llm_ready and search_ready else 503
        active_agent_settings = self._health_agent_settings()
        models_payload = self._resolve_health_models(
            self._providers.llm, self._providers.search, active_agent_settings
        )
        payload = {
            "status": "ok" if status_code == 200 else "degraded",
            "llm": {
                "provider": llm_label,
                "status": "ready" if llm_ready else "unavailable",
            },
            "search": {
                "provider": search_label,
                "status": "ready" if search_ready else "unavailable",
            },
            "testing_mode": active_agent_settings.testing_mode,
            "report_profile": str(active_agent_settings.report_profile),
            **models_payload,
            "high_risk_score_threshold": active_agent_settings.high_risk_score_threshold,
            "model_tier": active_agent_settings.model_tier,
            "auth_required": self._auth_provider.mode != "none",
            "auth_mode": self._auth_provider.mode,
            "version": __display_version__,
            "legal": legal_metadata(),
            "ai_disclosure": ai_disclosure_metadata(),
        }
        return status_code, payload

    @staticmethod
    def models_payload() -> dict[str, Any]:
        """Build the OpenAI-compatible ``/v1/models`` listing."""
        return {
            "object": "list",
            "data": [
                {
                    "id": MODEL_NAME,
                    "object": "model",
                    "created": 0,
                    "owned_by": "inqtrix",
                }
            ],
        }
