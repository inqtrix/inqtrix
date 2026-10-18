"""Node-level model/effort resolution wiring.

Covers the single ``_resolve_node_llm`` entry point every node uses, the
``node_model_resolution`` forensic event, the per-run ``model_tier`` selection,
and the HTTP override that feeds it.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from inqtrix.nodes import (
    _claim_extract_accepts_routing,
    _resolve_answer_fallback_model,
    _resolve_node_llm,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.providers.litellm import LiteLLM
from inqtrix.server.overrides import AgentOverridesRequest, apply_overrides
from inqtrix.settings import AgentSettings
from inqtrix.state import initial_state


def _providers(**model_kwargs) -> ProviderContext:
    llm = LiteLLM(api_key="k", **model_kwargs)
    return ProviderContext(llm=llm, search=None)


def _forensic_settings(**kwargs) -> AgentSettings:
    return AgentSettings(observability_profile="forensic", testing_mode=True, **kwargs)


def test_resolve_node_llm_routes_tiers_per_node() -> None:
    providers = _providers(
        default_model="R", tier_high_model="H", tier_mid_model="M", tier_fast_model="F",
        tier_high_effort="high",
    )
    settings = _forensic_settings()
    s = initial_state("q")
    assert _resolve_node_llm(s, settings, providers, "answer") == ("H", "high")
    assert _resolve_node_llm(s, settings, providers, "plan") == ("M", "")
    assert _resolve_node_llm(s, settings, providers, "classify") == ("F", "")
    assert _resolve_node_llm(s, settings, providers, "direct_chat") == ("M", "")
    assert s["node_model_resolutions"]["direct_chat"] == {
        "node": "direct_chat",
        "model": "M",
        "tier": "mid",
        "effort": "",
        "model_source": "tier:mid",
        "effort_source": "provider_default",
        "requested_tier": "",
    }


def test_resolve_node_llm_emits_forensic_event() -> None:
    providers = _providers(default_model="R", tier_high_model="H", tier_high_effort="medium")
    settings = _forensic_settings()
    s = initial_state("q")
    _resolve_node_llm(s, settings, providers, "answer")
    events = [e for e in s["iteration_logs"] if e.get("event") == "node_model_resolution"]
    assert len(events) == 1
    event = events[0]
    assert event["node"] == "answer"
    assert event["model"] == "H"
    assert event["tier"] == "high"
    assert event["effort"] == "medium"


def test_resolve_node_llm_emits_run_event_with_provenance() -> None:
    providers = _providers(default_model="R", tier_high_model="H", tier_high_effort="medium")
    settings = _forensic_settings()
    s = initial_state("q")
    captured: list[tuple[str, dict]] = []
    s["_run_event_sink"] = lambda event_type, payload: captured.append((event_type, payload))

    _resolve_node_llm(s, settings, providers, "answer")

    resolution = [p for t, p in captured if t == "inqtrix.node.model_resolution"]
    assert len(resolution) == 1
    payload = resolution[0]
    assert payload["node"] == "answer"
    assert payload["model"] == "H"
    assert payload["tier"] == "high"
    assert payload["effort"] == "medium"
    assert payload["model_source"] == "tier:high"
    assert payload["effort_source"] == "tier:high"


def test_resolve_node_llm_run_event_marks_reasoning_model_default() -> None:
    # No tier set -> the reasoning_model default grips; the run-event must say so
    # rather than letting it look like a tier choice (No Silent Fallbacks).
    providers = _providers(default_model="R")
    settings = _forensic_settings()
    s = initial_state("q")
    captured: list[tuple[str, dict]] = []
    s["_run_event_sink"] = lambda event_type, payload: captured.append((event_type, payload))

    _resolve_node_llm(s, settings, providers, "answer")

    payload = next(p for t, p in captured if t == "inqtrix.node.model_resolution")
    assert payload["model"] == "R"
    assert payload["model_source"] == "reasoning_model_default"
    assert payload["effort"] == ""
    assert payload["effort_source"] == "provider_default"


def test_no_models_attribute_warns_loudly() -> None:
    class _Bare:
        def complete(self, *a, **k):  # pragma: no cover - not called
            return ""

    providers = ProviderContext(llm=_Bare(), search=None)
    settings = _forensic_settings()
    s = initial_state("q")
    captured: list[tuple[str, dict]] = []
    s["_run_event_sink"] = lambda event_type, payload: captured.append((event_type, payload))
    inqtrix_logger = logging.getLogger("inqtrix")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    inqtrix_logger.addHandler(handler)
    inqtrix_logger.setLevel(logging.WARNING)
    try:
        assert _resolve_node_llm(s, settings, providers, "answer") == ("", "")
    finally:
        inqtrix_logger.removeHandler(handler)
    assert any(
        rec.levelno >= logging.WARNING and "could not resolve a model" in rec.getMessage()
        for rec in records
    )
    resolution = [
        p for t, p in captured if t == "inqtrix.node.model_resolution"
    ]
    assert resolution == [
        {
            "node": "answer",
            "model": "",
            "tier": "high",
            "effort": "",
            "model_source": "provider_models_missing",
            "effort_source": "provider_default_unseen",
            "requested_tier": "",
        }
    ]
    progress = [p for t, p in captured if t == "inqtrix.progress.message"]
    assert progress and progress[0]["severity"] == "warning"
    warnings = [
        e for e in s["iteration_logs"]
        if e.get("event") == "node_model_resolution_warning"
    ]
    assert warnings and warnings[0]["reason"] == "provider_models_missing"


def test_empty_resolved_model_warns_loudly() -> None:
    from types import SimpleNamespace

    class _Provider:
        models = SimpleNamespace(reasoning_model="")

    providers = ProviderContext(llm=_Provider(), search=None)
    settings = _forensic_settings()
    s = initial_state("q")
    captured: list[tuple[str, dict]] = []
    s["_run_event_sink"] = lambda event_type, payload: captured.append((event_type, payload))
    inqtrix_logger = logging.getLogger("inqtrix")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    inqtrix_logger.addHandler(handler)
    inqtrix_logger.setLevel(logging.WARNING)
    try:
        model, _effort = _resolve_node_llm(s, settings, providers, "answer")
    finally:
        inqtrix_logger.removeHandler(handler)
    assert model == ""
    assert any(
        rec.levelno >= logging.WARNING and "resolved an empty model" in rec.getMessage()
        for rec in records
    )
    payload = next(p for t, p in captured if t == "inqtrix.node.model_resolution")
    assert payload["model"] == ""
    assert payload["model_source"] == "reasoning_model_default"
    progress = [p for t, p in captured if t == "inqtrix.progress.message"]
    assert progress and progress[0]["severity"] == "warning"
    warnings = [
        e for e in s["iteration_logs"]
        if e.get("event") == "node_model_resolution_warning"
    ]
    assert warnings and warnings[0]["reason"] == "empty_resolved_model"


def test_forensic_event_suppressed_without_forensic_profile() -> None:
    providers = _providers(default_model="R", tier_high_model="H")
    # Non-forensic profile -> the lineage event is gated off.
    settings = AgentSettings(testing_mode=True, observability_profile="summary")
    s = initial_state("q")
    _resolve_node_llm(s, settings, providers, "answer")
    assert not [e for e in s["iteration_logs"] if e.get("event") == "node_model_resolution"]


def test_resolve_node_llm_claim_extract_uses_fast_tier_and_emits_event() -> None:
    # claim_extract is routed like every other node: fast tier model + fast
    # tier effort, with a single visible resolution event (node="claim_extract").
    providers = _providers(
        default_model="R", tier_high_model="H", tier_mid_model="M", tier_fast_model="F",
        tier_fast_effort="none",
    )
    settings = _forensic_settings()
    s = initial_state("q")
    captured: list[tuple[str, dict]] = []
    s["_run_event_sink"] = lambda event_type, payload: captured.append((event_type, payload))

    model, effort = _resolve_node_llm(s, settings, providers, "claim_extract")

    assert (model, effort) == ("F", "none")
    payload = next(p for t, p in captured if t == "inqtrix.node.model_resolution")
    assert payload["node"] == "claim_extract"
    assert payload["model"] == "F"
    assert payload["tier"] == "fast"
    assert payload["effort"] == "none"


def test_model_tier_overrides_default_tier_for_every_node() -> None:
    providers = _providers(
        default_model="R", tier_high_model="H", tier_mid_model="M", tier_fast_model="F",
        tier_fast_effort="low",
    )
    settings = _forensic_settings(model_tier="fast")
    s = initial_state("q")
    # answer normally maps to high; the requested fast tier replaces that.
    assert _resolve_node_llm(s, settings, providers, "answer") == ("F", "low")
    assert _resolve_node_llm(s, settings, providers, "evaluate") == ("F", "low")


def test_per_node_model_override_still_wins_over_requested_tier() -> None:
    providers = _providers(default_model="R", tier_fast_model="F", answer_model="PINNED")
    settings = _forensic_settings(model_tier="fast")
    s = initial_state("q")
    model, _effort = _resolve_node_llm(s, settings, providers, "answer")
    assert model == "PINNED"


def test_no_models_attribute_returns_empty() -> None:
    class _Bare:
        def complete(self, *a, **k):  # pragma: no cover - not called
            return ""

    providers = ProviderContext(llm=_Bare(), search=None)
    settings = _forensic_settings()
    s = initial_state("q")
    assert _resolve_node_llm(s, settings, providers, "answer") == ("", "")


def test_answer_fallback_model_handles_provider_without_models() -> None:
    class _Bare:
        def complete(self, *a, **k):  # pragma: no cover - not called
            return ""

    providers = ProviderContext(llm=_Bare(), search=None)
    assert _resolve_answer_fallback_model(_forensic_settings(), providers, "answer") is None


def test_answer_fallback_model_respects_model_tier() -> None:
    providers = _providers(
        default_model="R", tier_high_model="H", tier_mid_model="M", tier_fast_model="F"
    )
    settings = _forensic_settings(model_tier="fast")
    assert _resolve_answer_fallback_model(settings, providers, "F") is None


def test_answer_fallback_model_uses_evaluate_resolution_when_distinct() -> None:
    providers = _providers(default_model="R", tier_high_model="H", tier_mid_model="M")
    assert _resolve_answer_fallback_model(_forensic_settings(), providers, "H") == "M"


# --------------------------------------------------------------------------- #
# Claim-extraction routing backward-compat (Baukasten)
# --------------------------------------------------------------------------- #


def test_claim_extract_accepts_routing_detects_signature() -> None:
    class _OldSignature:
        def extract(
            self, text, citations, question, *, deadline=None, provider_refs=None,
            text_char_limit=7000, citation_cap=8, max_claims=8, source_url_limit=4,
        ):  # pragma: no cover - signature only
            return [], 0, 0

    class _Kwargs:
        def extract(self, *args, **kwargs):  # pragma: no cover - signature only
            return [], 0, 0

    class _NewSignature:
        def extract(
            self, text, citations, question, *, deadline=None, provider_refs=None,
            text_char_limit=7000, citation_cap=8, max_claims=8, source_url_limit=4,
            model=None, reasoning_effort=None,
        ):  # pragma: no cover - signature only
            return [], 0, 0

    # Old fixed signature without the routing kwargs -> do NOT pass them.
    assert _claim_extract_accepts_routing(_OldSignature()) is False
    # **kwargs swallows anything -> safe to pass.
    assert _claim_extract_accepts_routing(_Kwargs()) is True
    # New explicit signature -> pass.
    assert _claim_extract_accepts_routing(_NewSignature()) is True


# --------------------------------------------------------------------------- #
# HTTP model_tier override
# --------------------------------------------------------------------------- #


def test_model_tier_override_flows_into_agent_settings() -> None:
    merged = apply_overrides(AgentSettings(), AgentOverridesRequest(model_tier="fast"))
    assert merged.model_tier == "fast"


def test_model_tier_override_rejects_invalid_value() -> None:
    with pytest.raises(ValidationError):
        AgentOverridesRequest(model_tier="bogus")


def test_no_model_tier_override_leaves_default() -> None:
    merged = apply_overrides(AgentSettings(), AgentOverridesRequest())
    assert merged.model_tier == ""


# --------------------------------------------------------------------------- #
# Config-boundary model_tier validation (env / Python construction)
# --------------------------------------------------------------------------- #


def test_agent_settings_rejects_invalid_model_tier() -> None:
    # A typo like MODEL_TIER=hgih must fail loudly, not silently fall back to
    # the default per-node assignment.
    with pytest.raises(ValidationError):
        AgentSettings(model_tier="hgih")


def test_agent_config_rejects_invalid_model_tier() -> None:
    from inqtrix.agent import AgentConfig

    with pytest.raises(ValidationError):
        AgentConfig(model_tier="bogus")


def test_model_tier_empty_and_valid_pass_and_canonicalize() -> None:
    from inqtrix.agent import AgentConfig

    assert AgentSettings(model_tier="").model_tier == ""
    assert AgentSettings(model_tier=" HIGH ").model_tier == "high"
    assert AgentConfig(model_tier="fast").model_tier == "fast"
