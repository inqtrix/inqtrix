"""Contract tests for ``/health``, ``/v1/models`` and the OpenAPI toggle.

Locks the discovery surface the React app boots from. New fields may be
ADDED to ``/health`` (the client tolerates unknown keys); removing or
renaming any key asserted here is a breaking change and must not happen
silently.
"""

from __future__ import annotations

from inqtrix.settings import AgentSettings, ServerSettings

from tests.contract._app import make_contract_client

# Every key the current /health payload carries. Additions are allowed
# (subset assertion below); removals/renames break the React client.
HEALTH_REQUIRED_KEYS = {
    "status",
    "llm",
    "search",
    "testing_mode",
    "report_profile",
    "reasoning_model",
    "search_model",
    "classify_model",
    "claim_extract_model",
    "evaluate_model",
    "node_models",
    "chat_model_options",
    "models_catalog",
    "context_window_tokens",
    "high_risk_score_threshold",
    "model_tier",
    "auth_required",
    "auth_mode",
    "version",
    "legal",
    "ai_disclosure",
}


def test_health_payload_carries_all_contract_keys():
    with make_contract_client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    missing = HEALTH_REQUIRED_KEYS - payload.keys()
    assert not missing, f"/health lost contract keys: {sorted(missing)}"
    assert payload["status"] == "ok"
    assert payload["llm"] == {"provider": "StubLLM", "status": "ready"}
    assert payload["search"] == {"provider": "StubSearch", "status": "ready"}
    assert payload["auth_required"] is False
    assert isinstance(payload["node_models"], dict)
    assert isinstance(payload["chat_model_options"], list)
    assert isinstance(payload["models_catalog"], list)
    assert payload["ai_disclosure"]["marker"] == "ai-generated"
    assert payload["ai_disclosure"]["producer"] == "Inqtrix"


def test_health_reports_auth_required_true_when_api_key_set():
    with make_contract_client(
        server_settings=ServerSettings(api_key="secret-token-123"),
    ) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["auth_required"] is True


def test_models_payload_is_byte_stable():
    with make_contract_client() as client:
        response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {
                "id": "research-agent",
                "object": "model",
                "created": 0,
                "owned_by": "inqtrix",
            }
        ],
    }


def test_testing_mode_flag_is_surfaced():
    with make_contract_client(
        agent_settings=AgentSettings(testing_mode=True),
    ) as client:
        response = client.get("/health")

    assert response.json()["testing_mode"] is True


def test_openapi_disabled_by_default():
    """Historical default: no schema, no docs routes."""
    with make_contract_client() as client:
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404


def test_openapi_enabled_via_server_setting():
    with make_contract_client(
        server_settings=ServerSettings(enable_openapi=True),
    ) as client:
        schema = client.get("/openapi.json")
        docs = client.get("/docs")

    assert schema.status_code == 200
    assert schema.json()["info"]["title"] == "Inqtrix Research Agent"
    paths = schema.json()["paths"]
    assert "/health" in paths
    assert "/v1/chat/completions" in paths
    assert docs.status_code == 200
