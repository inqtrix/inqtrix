"""Tests for per-request agent overrides.

Covers the whitelist Pydantic model, range validation, the merge
helper ``apply_overrides`` (including the three profile-switch
scenarios A/B/C from the ADR), and the end-to-end integration
through ``/v1/chat/completions``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import inqtrix.research.web_research as web_research_module
from inqtrix.providers.base import LLMResponse
from inqtrix.report_profiles import ReportProfile
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.overrides import (
    AgentOverridesRequest,
    apply_overrides,
    parse_overrides_payload,
)
from inqtrix.server.routes import create_router, register_routes
from inqtrix.server.runs import RunStore
from inqtrix.settings import AgentSettings, ModelSettings, ServerSettings, Settings


# ------------------------------------------------------------------ #
# Whitelist Pydantic model
# ------------------------------------------------------------------ #


def test_overrides_request_accepts_whitelist_fields():
    """All whitelist fields must validate when supplied with sane values."""
    overrides = AgentOverridesRequest(
        max_rounds=4,
        min_rounds=2,
        confidence_stop=8,
        report_profile=ReportProfile.DEEP,
        max_total_seconds=540,
        first_round_queries=8,
    )
    assert overrides.max_rounds == 4
    assert overrides.report_profile == ReportProfile.DEEP


def test_overrides_request_rejects_unknown_field():
    """Unknown keys must raise (model_config extra='forbid')."""
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"unknown_field": 1})


def test_overrides_request_validates_ranges():
    """Range-violating values must raise."""
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"max_rounds": 99})
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"max_rounds": 5})
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"first_round_queries": 9})
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"max_total_seconds": 5})  # ge=30
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"confidence_stop": 0})  # ge=1


def test_parse_overrides_payload_returns_none_when_absent():
    assert parse_overrides_payload(None) is None


def test_parse_overrides_payload_rejects_non_dict():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        parse_overrides_payload("not a dict")
    assert excinfo.value.status_code == 400


def test_parse_overrides_payload_rejects_unknown_field():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        parse_overrides_payload({"definitely_not_real": 1})
    assert excinfo.value.status_code == 400


# ------------------------------------------------------------------ #
# apply_overrides — base behaviour
# ------------------------------------------------------------------ #


def test_apply_overrides_no_op_when_none():
    base = AgentSettings()
    assert apply_overrides(base, None) is base


def test_apply_overrides_no_op_when_empty():
    base = AgentSettings()
    overrides = AgentOverridesRequest()
    assert apply_overrides(base, overrides) is base


def test_env_agent_tier_bridges_depth_without_request_overrides():
    """An operator-level AGENT_TIER must resolve to a CONSISTENT
    (tier, depth) pair — the durable replay body re-parses the echoed
    pair and a contradiction would 400 every run at worker claim time."""
    base = AgentSettings(AGENT_TIER="tief")
    resolved = apply_overrides(base, None)
    assert resolved.agent_tier == "tief"
    assert resolved.depth == "deep"


def test_env_agent_tier_bridge_with_unrelated_request_override():
    base = AgentSettings(AGENT_TIER="tief")
    resolved = apply_overrides(base, AgentOverridesRequest(max_rounds=3))
    assert resolved.agent_tier == "tief"
    assert resolved.depth == "deep"
    assert resolved.max_rounds == 3


def test_request_depth_wins_over_contradicting_env_tier():
    """Explicit request depth beats the env default tier (request beats
    env everywhere); the run keeps legacy depth semantics, visibly."""
    base = AgentSettings(AGENT_TIER="gruendlich")
    resolved = apply_overrides(base, AgentOverridesRequest(depth="deep"))
    assert resolved.agent_tier == ""
    assert resolved.depth == "deep"


def test_apply_overrides_merges_into_agent_settings():
    base = AgentSettings()
    overrides = AgentOverridesRequest(max_rounds=2, confidence_stop=6)
    patched = apply_overrides(base, overrides)
    assert patched is not base
    assert patched.max_rounds == 2
    assert patched.confidence_stop == 6


# ------------------------------------------------------------------ #
# Profile-switch semantics — scenarios A / B / C
# ------------------------------------------------------------------ #


def test_apply_overrides_preserves_operator_explicit_fields():
    """Scenario A — operator-explicit max_rounds must survive a DEEP switch."""
    # Operator explicitly sets max_rounds=6 against the COMPACT default.
    base = AgentSettings(report_profile=ReportProfile.COMPACT, max_rounds=6)
    overrides = AgentOverridesRequest(report_profile=ReportProfile.DEEP)
    patched = apply_overrides(base, overrides)

    assert patched.report_profile == ReportProfile.DEEP
    # Operator-explicit value wins over the DEEP profile default of 3.
    assert patched.max_rounds == 6
    # Non-explicit fields take on DEEP defaults.
    assert patched.confidence_stop == 7
    assert patched.first_round_queries == 6


def test_apply_overrides_applies_full_profile_defaults():
    """Scenario B — pure COMPACT defaults flip wholesale to DEEP."""
    # Use Pydantic-default construction so model_fields_set stays empty.
    base = AgentSettings.model_validate({})
    overrides = AgentOverridesRequest(report_profile=ReportProfile.DEEP)
    patched = apply_overrides(base, overrides)

    assert patched.report_profile == ReportProfile.DEEP
    # All DEEP-profile defaults filled in.
    assert patched.max_rounds == 3
    assert patched.confidence_stop == 7
    assert patched.first_round_queries == 6


def test_apply_overrides_schnell_runs_exactly_one_round():
    """SCHNELL keeps one round while sharing robust operation budgets."""
    base = AgentSettings.model_validate({})
    overrides = AgentOverridesRequest(report_profile=ReportProfile.SCHNELL)
    patched = apply_overrides(base, overrides)

    assert patched.report_profile == ReportProfile.SCHNELL
    assert patched.max_rounds == 1
    assert patched.min_rounds == 1
    assert patched.first_round_queries == 6
    assert patched.max_total_seconds == 3600

    # Bidirectional switch: the same base flips wholesale to DEEP.
    deepened = apply_overrides(
        base, AgentOverridesRequest(report_profile=ReportProfile.DEEP)
    )
    assert deepened.max_rounds == 3


def test_apply_overrides_user_explicit_wins_over_profile_defaults():
    """Scenario C — user-explicit max_rounds beats DEEP-profile preset."""
    base = AgentSettings.model_validate({})
    overrides = AgentOverridesRequest(
        report_profile=ReportProfile.DEEP, max_rounds=4
    )
    patched = apply_overrides(base, overrides)

    assert patched.report_profile == ReportProfile.DEEP
    # User-explicit value wins over the DEEP default of 3.
    assert patched.max_rounds == 4
    # Other DEEP defaults are still applied.
    assert patched.confidence_stop == 7
    assert patched.first_round_queries == 6


def test_apply_overrides_can_switch_deep_base_back_to_compact_defaults():
    """A compact profile switch resets non-explicit fields from a DEEP base."""
    base = AgentSettings(report_profile=ReportProfile.DEEP)
    overrides = AgentOverridesRequest(report_profile=ReportProfile.COMPACT)
    patched = apply_overrides(base, overrides)

    assert patched.report_profile == ReportProfile.COMPACT
    assert patched.max_rounds == 2
    assert patched.min_rounds == 1
    assert patched.confidence_stop == 7
    assert patched.first_round_queries == 6
    assert patched.answer_prompt_citations_max == 60
    assert patched.reasoning_timeout == 600
    assert patched.editor_assistant_timeout == 600
    assert patched.claim_extract_timeout == 600
    assert patched.search_timeout == 600
    assert patched.max_total_seconds == 3600


# ------------------------------------------------------------------ #
# Integration through /v1/chat/completions
# ------------------------------------------------------------------ #


class _DummyLLM:
    def complete(self, *args, **kwargs):
        return "ok"


    def is_available(self) -> bool:
        return True


class _RoutingLLM:
    """Fake LLM that records the model kwargs used by direct chat."""

    def __init__(self, *, direct_chat_model: str = "") -> None:
        self.models = ModelSettings(
            reasoning_model="R",
            direct_chat_model=direct_chat_model,
            tier_high_model="H",
            tier_mid_model="M",
            tier_fast_model="F",
            tier_high_effort="medium",
            tier_mid_effort="none",
            tier_fast_effort="none",
        )
        self.calls: list[dict[str, Any]] = []

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return "Antwort"

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        self.calls.append(dict(kwargs))
        return LLMResponse(
            content="Antwort",
            prompt_tokens=2,
            completion_tokens=3,
            model=str(kwargs.get("model") or ""),
        )

    def is_available(self) -> bool:
        return True


class _DummySearch:
    def search(self, *args, **kwargs):
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


def _make_app(
    *,
    agent_settings: AgentSettings | None = None,
    run_store: RunStore | None = None,
) -> tuple[TestClient, dict[str, Any], Any]:
    """Build a TestClient with a stub agent_run that records the settings."""
    captured: dict[str, Any] = {}

    def fake_run(
        question: str,
        *,
        history: str,
        providers: Any,
        strategies: Any,
        settings: AgentSettings,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["settings"] = settings
        captured["question"] = question
        return {
            "answer": "Antwort",
            "result_state": {},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=agent_settings or AgentSettings(),
        server=ServerSettings(),
    )
    providers = SimpleNamespace(llm=_DummyLLM(), search=_DummySearch())

    register_routes(
        router,
        providers=providers,
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        run_store=run_store,
    )
    app.include_router(router)

    client = TestClient(app)
    client.app.dependency_overrides = {}  # noqa: SLF001 — placeholder
    return client, captured, fake_run


def _make_routing_app(llm: _RoutingLLM) -> TestClient:
    """Build a TestClient that exercises the real direct-chat graph path."""
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
    )
    providers = SimpleNamespace(llm=llm, search=_DummySearch())

    register_routes(
        router,
        providers=providers,
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )
    app.include_router(router)
    return TestClient(app)


def test_chat_completions_with_overrides_routes_through_agent_run(monkeypatch):
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"max_rounds": 2, "confidence_stop": 7},
        },
    )

    assert response.status_code == 200
    forwarded = captured["settings"]
    assert forwarded.max_rounds == 2
    assert forwarded.confidence_stop == 7


def test_chat_completions_with_profile_switch_routes_deep_settings(monkeypatch):
    """End-to-end Scenario B: pure COMPACT base + DEEP override."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"report_profile": "deep"},
        },
    )

    assert response.status_code == 200
    forwarded = captured["settings"]
    assert forwarded.report_profile == ReportProfile.DEEP
    # DEEP profile defaults must have cascaded.
    assert forwarded.max_rounds == 3
    assert forwarded.confidence_stop == 7
    assert forwarded.first_round_queries == 6


def test_chat_completions_invalid_override_returns_400(monkeypatch):
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"max_rounds": 99},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    # agent_run must NOT be invoked when validation fails.
    assert "settings" not in captured


def test_chat_completions_without_overrides_uses_server_defaults(monkeypatch):
    """No agent_overrides field → server-default AgentSettings reaches agent_run."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
        },
    )

    assert response.status_code == 200
    forwarded = captured["settings"]
    # Server default profile is COMPACT.
    assert forwarded.report_profile == ReportProfile.COMPACT


def test_chat_completions_direct_mode_sets_skip_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level mode=direct_llm maps to the existing skip_search bypass."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "direct_llm",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert captured["settings"].skip_search is True


def test_chat_completions_research_mode_overrides_global_skip_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mode=research must force the graph path even when the server default skips search."""
    client, captured, fake_run = _make_app(
        agent_settings=AgentSettings(skip_search=True)
    )
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "research",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
        },
    )

    assert response.status_code == 200
    assert captured["settings"].skip_search is False


def test_chat_completions_rejects_conflicting_direct_mode_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conflicting mode and legacy skip_search override should fail loudly."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "direct_llm",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"skip_search": False},
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "settings" not in captured


def test_chat_completions_legacy_skip_search_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing clients can keep using agent_overrides.skip_search."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"skip_search": True},
        },
    )

    assert response.status_code == 200
    assert captured["settings"].skip_search is True


@pytest.mark.parametrize(
    "tier, expected_model, expected_effort",
    [
        ("fast", "F", "none"),
        ("high", "H", "medium"),
    ],
)
def test_chat_completions_direct_mode_uses_selected_model_tier(
    tier: str,
    expected_model: str,
    expected_effort: str,
) -> None:
    llm = _RoutingLLM()
    client = _make_routing_app(llm)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "direct_llm",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"model_tier": tier},
        },
    )

    assert response.status_code == 200
    assert llm.calls[-1]["model"] == expected_model
    assert llm.calls[-1]["reasoning_effort"] == expected_effort
    model_resolution = response.json()["inqtrix"]["model_resolution"]
    assert model_resolution["node"] == "direct_chat"
    assert model_resolution["model"] == expected_model
    assert model_resolution["effort"] == expected_effort
    assert model_resolution["requested_tier"] == tier


def test_chat_completions_direct_mode_respects_direct_chat_override() -> None:
    llm = _RoutingLLM(direct_chat_model="PINNED")
    client = _make_routing_app(llm)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "direct_llm",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
            "agent_overrides": {"model_tier": "fast"},
        },
    )

    assert response.status_code == 200
    assert llm.calls[-1]["model"] == "PINNED"
    assert llm.calls[-1]["reasoning_effort"] == "none"
    model_resolution = response.json()["inqtrix"]["model_resolution"]
    assert model_resolution["model"] == "PINNED"
    assert model_resolution["model_source"] == "per_node_override"
    assert model_resolution["requested_tier"] == "fast"


def test_chat_completions_rejects_invalid_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown mode values should not silently fall back to research."""
    client, captured, fake_run = _make_app()
    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/chat/completions",
        json={
            "mode": "chat",
            "messages": [{"role": "user", "content": "Hallo"}],
            "stream": False,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "settings" not in captured


def test_native_runs_direct_mode_returns_mode_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native UI runs expose the resolved mode and forward skip_search."""
    run_store = RunStore(
        max_concurrent=1,
        max_queue_size=1,
        completed_ttl_seconds=30,
        event_buffer_size=10,
    )
    client, captured, _fake_run = _make_app(run_store=run_store)
    observed = threading.Event()

    def fake_run(
        question: str,
        *,
        history: str,
        providers: Any,
        strategies: Any,
        settings: AgentSettings,
        **kwargs: Any,
    ) -> dict[str, Any]:
        captured["settings"] = settings
        captured["question"] = question
        observed.set()
        return {
            "answer": "Antwort",
            "result_state": {"done": True},
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    response = client.post(
        "/v1/runs",
        json={
            "mode": "direct_llm",
            "messages": [{"role": "user", "content": "Hallo"}],
        },
    )

    assert response.status_code == 202
    assert response.json()["mode"] == "direct_llm"
    assert observed.wait(timeout=1)
    assert captured["settings"].skip_search is True


def test_depth_override_validates_and_merges():
    """`depth` rides the generic whitelist: 'deep' merges
    into AgentSettings, anything outside the vocabulary raises."""
    overrides = AgentOverridesRequest.model_validate({"depth": "deep"})
    merged = apply_overrides(AgentSettings(), overrides)
    assert merged.depth == "deep"
    assert AgentSettings().depth == "normal"
    with pytest.raises(ValidationError):
        AgentOverridesRequest.model_validate({"depth": "ultra"})
