"""Tests for the multi-stack server."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.stacks import (
    StackBundle,
    _DiscoveryCache,
    _validate_stacks,
    create_multi_stack_app,
)
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    SharingSettings,
)


# ------------------------------------------------------------------ #
# Stub providers (network-free)
# ------------------------------------------------------------------ #


class _StubLLM:
    def __init__(self, label: str = "default") -> None:
        self._label = label

    def complete(self, *a, **kw):
        return self._label


    def is_available(self):
        return True


class _StubSearch:
    def __init__(self, label: str = "default") -> None:
        self._label = label

    def search(self, *a, **kw):
        return GroundedSearchResult()

    def is_available(self):
        return True


def _bundle(label: str, *, description: str = "") -> StackBundle:
    return StackBundle(
        providers=ProviderContext(llm=_StubLLM(label), search=_StubSearch(label)),
        description=description,
    )


def _make_settings() -> Settings:
    return Settings(models=ModelSettings(), agent=AgentSettings(), server=ServerSettings())


# ------------------------------------------------------------------ #
# StackBundle + validation
# ------------------------------------------------------------------ #


def test_stack_bundle_is_frozen():
    bundle = _bundle("a")
    with pytest.raises(Exception):  # FrozenInstanceError or TypeError
        bundle.description = "mutated"


def test_validate_stacks_rejects_empty():
    with pytest.raises(ValueError, match="at least one stack"):
        _validate_stacks({}, "x")


def test_validate_stacks_rejects_unknown_default():
    with pytest.raises(ValueError, match="not in stacks"):
        _validate_stacks({"a": _bundle("a")}, "missing")


def test_validate_stacks_rejects_invalid_name():
    with pytest.raises(ValueError, match="must match"):
        _validate_stacks({"BadName": _bundle("a")}, "BadName")


def test_validate_stacks_accepts_lowercase_underscored_names():
    _validate_stacks(
        {"valid_name_123": _bundle("a")}, "valid_name_123"
    )  # must not raise


# ------------------------------------------------------------------ #
# Discovery endpoint
# ------------------------------------------------------------------ #


def test_discovery_endpoint_lists_all_stacks_with_ready_flag():
    settings = _make_settings()
    stacks = {
        "stack_a": _bundle("a", description="Stack A description"),
        "stack_b": _bundle("b", description="Stack B description"),
    }
    app = create_multi_stack_app(
        settings=settings, stacks=stacks, default_stack="stack_a"
    )
    with TestClient(app) as client:
        response = client.get("/v1/stacks")
    assert response.status_code == 200
    payload = response.json()
    assert payload["default"] == "stack_a"
    assert len(payload["stacks"]) == 2
    names = {s["name"] for s in payload["stacks"]}
    assert names == {"stack_a", "stack_b"}
    for s in payload["stacks"]:
        assert s["ready"] is True
        assert s["llm"] == "_StubLLM"
        assert s["search"] == "_StubSearch"


def test_restricted_sharing_reconciles_in_multi_stack_lifespan():
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        sharing=SharingSettings(restrict_to_workspace_members=True),
    )
    app = create_multi_stack_app(
        settings=settings,
        stacks={"default": _bundle("default")},
        default_stack="default",
    )
    calls: list[str] = []

    async def reconcile(*, tenant_id: str) -> int:
        calls.append(tenant_id)
        return 0

    app.state.container.workspace_admin.reconcile_workspace_shares = reconcile

    with TestClient(app):
        pass

    assert calls == ["default"]


def test_discovery_payload_includes_per_stack_models_block():
    """Bug C regression for multi-stack: every entry in /v1/stacks must
    expose a ``models`` block built from the bundle's own provider, so a
    UI can render an honest per-stack model chip without an extra
    /health round-trip.
    """

    class _LLMWithModels(_StubLLM):
        def __init__(self, label: str, claim_extract: str) -> None:
            super().__init__(label=label)
            self.models = ModelSettings(
                reasoning_model=f"reason-{label}",
                claim_extract_model=claim_extract,
            )

    class _SearchWithModel(_StubSearch):
        def __init__(self, label: str, model: str) -> None:
            super().__init__(label=label)
            # The standardized search_model property is used by
            # ``_stack_models_payload`` (no more attribute heuristic).
            self.search_model = model

    settings = _make_settings()
    bundle_a = StackBundle(
        providers=ProviderContext(
            llm=_LLMWithModels("a", "haiku-a"),
            search=_SearchWithModel("a", "sonar-a"),
        ),
        description="A",
    )
    bundle_b = StackBundle(
        providers=ProviderContext(
            llm=_LLMWithModels("b", "haiku-b"),
            search=_SearchWithModel("b", "sonar-b"),
        ),
        description="B",
    )
    stacks = {"stack_a": bundle_a, "stack_b": bundle_b}
    app = create_multi_stack_app(
        settings=settings, stacks=stacks, default_stack="stack_a"
    )
    with TestClient(app) as client:
        payload = client.get("/v1/stacks").json()

    by_name = {s["name"]: s for s in payload["stacks"]}
    models_a = by_name["stack_a"]["models"]
    assert models_a["reasoning_model"] == "reason-a"
    assert models_a["claim_extract_model"] == "haiku-a"
    assert models_a["classify_model"] == "reason-a"
    assert models_a["evaluate_model"] == "reason-a"
    assert models_a["search_model"] == "sonar-a"
    # Per-node resolution block with provenance (same shape as /health):
    # answer has no tier so the reasoning_model default grips (visibly),
    # claim_extract is a per-node override.
    assert models_a["node_models"]["answer"]["model"] == "reason-a"
    assert models_a["node_models"]["answer"]["model_source"] == "reasoning_model_default"
    assert models_a["node_models"]["claim_extract"]["model"] == "haiku-a"
    assert models_a["node_models"]["claim_extract"]["model_source"] == "per_node_override"
    assert [option["tier"] for option in models_a["chat_model_options"]] == [
        "high",
        "mid",
        "fast",
    ]
    assert [option["node"] for option in models_a["chat_model_options"]] == [
        "direct_chat",
        "direct_chat",
        "direct_chat",
    ]
    assert by_name["stack_b"]["models"]["claim_extract_model"] == "haiku-b"
    assert by_name["stack_b"]["models"]["search_model"] == "sonar-b"


def test_discovery_payload_respects_stack_specific_model_tier():
    class _LLMWithTiers(_StubLLM):
        def __init__(self, label: str) -> None:
            super().__init__(label=label)
            self.models = ModelSettings(
                reasoning_model=f"fallback-{label}",
                tier_high_model=f"high-{label}",
                tier_mid_model=f"mid-{label}",
                tier_fast_model=f"fast-{label}",
            )

    settings = _make_settings()
    bundle_a = StackBundle(
        providers=ProviderContext(
            llm=_LLMWithTiers("a"),
            search=_StubSearch("a"),
        ),
        agent_settings=AgentSettings(model_tier="fast"),
    )
    bundle_b = StackBundle(
        providers=ProviderContext(
            llm=_LLMWithTiers("b"),
            search=_StubSearch("b"),
        ),
    )
    app = create_multi_stack_app(
        settings=settings,
        stacks={"stack_a": bundle_a, "stack_b": bundle_b},
        default_stack="stack_a",
    )
    with TestClient(app) as client:
        payload = client.get("/v1/stacks").json()

    by_name = {s["name"]: s for s in payload["stacks"]}
    models_a = by_name["stack_a"]["models"]
    models_b = by_name["stack_b"]["models"]
    assert models_a["node_models"]["answer"]["model"] == "fast-a"
    assert models_a["node_models"]["answer"]["requested_tier"] == "fast"
    assert models_a["evaluate_model"] == "fast-a"
    assert [option["model"] for option in models_a["chat_model_options"]] == [
        "high-a",
        "mid-a",
        "fast-a",
    ]
    assert models_b["node_models"]["answer"]["model"] == "high-b"
    assert models_b["node_models"]["answer"]["requested_tier"] == ""


def test_health_payload_uses_default_stack_agent_settings_model_tier():
    class _LLMWithTiers(_StubLLM):
        def __init__(self) -> None:
            super().__init__(label="default")
            self.models = ModelSettings(
                reasoning_model="fallback",
                tier_high_model="high-model",
                tier_fast_model="fast-model",
            )

    bundle = StackBundle(
        providers=ProviderContext(
            llm=_LLMWithTiers(),
            search=_StubSearch("default"),
        ),
        agent_settings=AgentSettings(model_tier="fast"),
    )
    app = create_multi_stack_app(
        settings=_make_settings(),
        stacks={"stack_a": bundle},
        default_stack="stack_a",
    )
    with TestClient(app) as client:
        payload = client.get("/health").json()

    assert payload["model_tier"] == "fast"
    assert payload["node_models"]["answer"]["model"] == "fast-model"


def test_discovery_payload_marks_missing_provider_models_without_settings_leak():
    settings = Settings(
        models=ModelSettings(reasoning_model="settings-default"),
        agent=AgentSettings(),
        server=ServerSettings(),
    )
    app = create_multi_stack_app(
        settings=settings,
        stacks={"stack_a": _bundle("a")},
        default_stack="stack_a",
    )
    with TestClient(app) as client:
        payload = client.get("/v1/stacks").json()

    models = payload["stacks"][0]["models"]
    assert models["reasoning_model"] == ""
    assert models["classify_model"] == ""
    assert models["claim_extract_model"] == ""
    assert models["evaluate_model"] == ""
    assert models["node_models"]["answer"]["model_source"] == "provider_models_missing"
    assert models["node_models"]["answer"]["effort_source"] == "provider_default_unseen"
    assert all(option["model"] == "" for option in models["chat_model_options"])
    assert all(
        option["model_source"] == "provider_models_missing"
        for option in models["chat_model_options"]
    )


def test_discovery_cache_returns_same_payload_within_ttl():
    """The same StackBundle.is_available() must not be hammered."""
    cache = _DiscoveryCache()
    stacks = {"a": _bundle("a"), "b": _bundle("b")}
    settings = AgentSettings()
    p1 = cache.get(stacks=stacks, default_stack="a", default_agent_settings=settings)
    p2 = cache.get(stacks=stacks, default_stack="a", default_agent_settings=settings)
    assert p1 is p2  # cached object identity


# ------------------------------------------------------------------ #
# Multi-stack chat-completions routing
# ------------------------------------------------------------------ #


def _patched_agent_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch agent_run to record the providers/settings it received."""
    captured: dict[str, Any] = {}

    def fake_run(
        question,
        *,
        history,
        providers,
        strategies,
        settings,
        cancel_event,
    ):
        assert cancel_event is not None and not cancel_event.is_set()
        captured["llm_label"] = providers.llm._label
        captured["settings_max_rounds"] = settings.max_rounds
        return {
            "answer": f"answer from {providers.llm._label}",
            "result_state": {},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)
    return captured


def test_chat_completions_routes_to_default_stack_when_no_field(monkeypatch):
    captured = _patched_agent_run(monkeypatch)
    settings = _make_settings()
    stacks = {
        "alpha": _bundle("alpha", description="Alpha"),
        "beta": _bundle("beta", description="Beta"),
    }
    app = create_multi_stack_app(settings=settings, stacks=stacks, default_stack="alpha")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}], "stream": False},
        )
    assert response.status_code == 200
    assert captured["llm_label"] == "alpha"


def test_chat_completions_routes_to_named_stack(monkeypatch):
    captured = _patched_agent_run(monkeypatch)
    settings = _make_settings()
    stacks = {"alpha": _bundle("alpha"), "beta": _bundle("beta")}
    app = create_multi_stack_app(settings=settings, stacks=stacks, default_stack="alpha")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "stack": "beta",
            },
        )
    assert response.status_code == 200
    assert captured["llm_label"] == "beta"


def test_chat_completions_unknown_stack_returns_400_with_available(monkeypatch):
    _patched_agent_run(monkeypatch)
    settings = _make_settings()
    stacks = {"alpha": _bundle("alpha"), "beta": _bundle("beta")}
    app = create_multi_stack_app(settings=settings, stacks=stacks, default_stack="alpha")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
                "stack": "does_not_exist",
            },
        )
    assert response.status_code == 400
    payload = response.json()
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["available_stacks"] == ["alpha", "beta"]


def test_chat_completions_per_stack_agent_settings_override_global(monkeypatch):
    """A StackBundle with its own agent_settings overrides settings.agent."""
    captured = _patched_agent_run(monkeypatch)
    settings = _make_settings()
    custom_settings = AgentSettings(max_rounds=2)  # global default is 4
    stacks = {
        "tight": StackBundle(
            providers=ProviderContext(llm=_StubLLM("tight"), search=_StubSearch("tight")),
            agent_settings=custom_settings,
        ),
    }
    app = create_multi_stack_app(settings=settings, stacks=stacks, default_stack="tight")
    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hi"}], "stream": False},
        )
    assert response.status_code == 200
    assert captured["settings_max_rounds"] == 2


def test_multi_stack_lifespan_owns_the_upload_reconciler():
    """The multi-stack factory shares the single-stack recovery contract:
    the lifespan starts upload reconciliation and stops it on shutdown."""
    app = create_multi_stack_app(
        settings=_make_settings(),
        stacks={"alpha": _bundle("alpha")},
        default_stack="alpha",
    )
    reconciler = app.state.container.upload_reconciler
    assert reconciler is not None
    assert reconciler.running is False
    with TestClient(app):
        assert reconciler.running is True
    assert reconciler.running is False
    reconciler.close()
