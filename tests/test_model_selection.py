"""Tests for explicit model/effort selection.

Covers the explicit-model short-circuit in the central resolver, the per-run
``model``/``effort`` override surface (AgentSettings/AgentConfig/overrides), the
provider ``selectable_models`` property, and the scoping rule that an explicit
model only affects the direct-chat call -- research nodes keep tier routing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from inqtrix.agent import AgentConfig, ProviderContext
from inqtrix.model_routing import describe_resolution, resolve_effort, resolve_model
from inqtrix.nodes import _resolve_node_llm
from inqtrix.providers.base import LLMProvider
from inqtrix.providers.litellm import LiteLLM
from inqtrix.server.overrides import AgentOverridesRequest, apply_overrides
from inqtrix.settings import AgentSettings, ModelSettings


# --------------------------------------------------------------------------- #
# Explicit-model resolution
# --------------------------------------------------------------------------- #

def test_explicit_model_short_circuits_with_provenance() -> None:
    """An explicit model wins over tier routing and is marked explicit_request."""
    models = ModelSettings(reasoning_model="tier-default", tier_mid_model="mid-x")
    desc = describe_resolution(
        "direct_chat", models, "mid",
        requested_model="claude-opus-4-8", requested_effort="xhigh",
    )
    assert desc["model"] == "claude-opus-4-8"
    assert desc["model_source"] == "explicit_request"
    assert desc["effort"] == "xhigh"
    assert desc["effort_source"] == "explicit_request"
    assert desc["requested_tier"] == "mid"


def test_explicit_model_without_effort_inherits_provider_default() -> None:
    """Explicit model with no effort leaves effort empty / provider_default."""
    desc = describe_resolution("direct_chat", None, None, requested_model="gpt-5.4")
    assert desc["model"] == "gpt-5.4"
    assert desc["effort"] == ""
    assert desc["effort_source"] == "provider_default"


def test_tier_path_unchanged_without_explicit_model() -> None:
    """Without requested_model the historical tier resolution is untouched."""
    models = ModelSettings(reasoning_model="reasoner")
    assert describe_resolution("answer", models)["model"] == "reasoner"


def test_resolve_wrappers_pass_explicit_model_through() -> None:
    """The thin wrappers forward requested_model/effort to describe_resolution."""
    models = ModelSettings(reasoning_model="reasoner")
    assert resolve_model("direct_chat", models, requested_model="m-x") == "m-x"
    assert resolve_effort(
        "direct_chat", models, requested_model="m-x", requested_effort="low"
    ) == "low"


# --------------------------------------------------------------------------- #
# Per-run override surface
# --------------------------------------------------------------------------- #

def test_agent_settings_and_config_expose_model_and_effort() -> None:
    """The new override fields exist and default to empty strings."""
    assert AgentSettings().model == "" and AgentSettings().effort == ""
    assert AgentConfig().model == "" and AgentConfig().effort == ""


def test_overrides_flow_model_and_effort_into_settings() -> None:
    """agent_overrides.model/effort merge into AgentSettings by field name."""
    overrides = AgentOverridesRequest.model_validate(
        {"model": "claude-opus-4-8", "effort": "high"}
    )
    merged = apply_overrides(AgentSettings(), overrides)
    assert merged.model == "claude-opus-4-8"
    assert merged.effort == "high"


def test_overrides_reject_invalid_effort_and_empty_model() -> None:
    """Bad effort and empty model are rejected at the HTTP boundary."""
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"effort": "turbo"})
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"model": ""})


# --------------------------------------------------------------------------- #
# selectable_models
# --------------------------------------------------------------------------- #

def test_llm_provider_abc_default_selectable_models_is_empty() -> None:
    """A provider that does not override selectable_models exposes []."""

    class _Bare(LLMProvider):
        def complete(self, prompt, **kwargs):  # type: ignore[override]
            return ""

        def is_available(self) -> bool:
            return True

    assert _Bare().selectable_models == []


def test_litellm_stores_and_exposes_selectable_models() -> None:
    """The LiteLLM provider accepts and returns a curated selectable list."""
    llm = LiteLLM(api_key="x", selectable_models=["gpt-5.4", "gpt-5.4-mini"])
    assert llm.selectable_models == ["gpt-5.4", "gpt-5.4-mini"]
    assert LiteLLM(api_key="x").selectable_models == []


# --------------------------------------------------------------------------- #
# Scoping: explicit model affects direct_chat only
# --------------------------------------------------------------------------- #

class _StubLLM:
    """Minimal LLM exposing ``.models`` for the node resolver."""

    def __init__(self) -> None:
        self.models = ModelSettings(reasoning_model="tier-model")

    def complete(self, *args, **kwargs):  # pragma: no cover - not called here
        return ""

    def is_available(self) -> bool:
        return True


def _resolve(node: str, settings: AgentSettings) -> tuple[str, str]:
    providers = ProviderContext(llm=_StubLLM(), search=None)
    return _resolve_node_llm({}, settings, providers, node)


def test_direct_chat_honours_explicit_model() -> None:
    """The direct-chat node uses an explicit model + effort from settings."""
    model, effort = _resolve("direct_chat", AgentSettings(model="picked-x", effort="high"))
    assert model == "picked-x"
    assert effort == "high"


def test_research_nodes_ignore_explicit_model() -> None:
    """Research nodes stay on tier routing even when an explicit model is set."""
    model, _ = _resolve("answer", AgentSettings(model="picked-x", effort="high"))
    assert model == "tier-model"
