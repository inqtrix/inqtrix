"""The request body at the API boundary: one JSON object, booleans as booleans.

A JSON route reads its body as one JSON object. A body that parses but is no
object (an array, a string, ``true``, ``null``) is a client error of the same
kind as a body that does not parse, so the route answers both alike: HTTP 400
and ``{"error": {"message": "Invalid JSON body", "type":
"invalid_request_error"}}``. A route whose fields are all optional also
accepts no body at all; a body that is sent is read the same way. A boolean
field takes only JSON ``true`` or ``false``, and its default holds when the
field is left out.

The malformed request on the same route is the anchor of the non-object
cases: it proves the request passed the sign-in and the CSRF check and
reached the body, so the refusal comes from the body check and from nothing
before it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import inqtrix.research.web_research as web_research_module
from inqtrix.auth import log_redaction
from inqtrix.providers.base import ProviderContext
from inqtrix.server.app import create_app
from inqtrix.settings import (
    AgentSettings,
    AuthSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import (
    StubLLM,
    StubSearch,
    make_contract_client,
    minimal_agent_result,
)
from tests.test_workspace_routes import login as workspace_login
from tests.test_workspace_routes import make_world as make_workspace_world

OWNER = ("owner@example.com", "correct-horse-battery")
USER_ID = "00000000-0000-4000-8000-000000000001"
WORKSPACE_ID = "ws-unknown-1"
MALFORMED = b"{not json"
# Spelled out here, not imported: a reworded refusal must fail this test.
INVALID_JSON_BODY_ENVELOPE = {
    "error": {"message": "Invalid JSON body", "type": "invalid_request_error"}
}

# Routes of the app a local-auth deployment serves; the caller is the
# first-run owner, an instance admin.
SERVER_ROUTES = [
    ("chat_completions", "POST", "/v1/chat/completions"),
    ("editor_suggest", "POST", "/v1/editor/suggest"),
    ("editor_instruct", "POST", "/v1/editor/instruct"),
    ("text_improvements", "POST", "/v1/text/improvements"),
    ("test_run", "POST", "/v1/test/run"),
    ("admin_user_role", "PATCH", f"/v1/admin/users/{USER_ID}"),
    ("admin_user_create", "POST", "/v1/admin/users"),
    ("admin_user_password", "POST", f"/v1/admin/users/{USER_ID}:reset-password"),
    ("admin_workspace_create", "POST", "/v1/admin/workspaces"),
    ("admin_workspace_rename", "PATCH", f"/v1/admin/workspaces/{WORKSPACE_ID}"),
    ("admin_member_add", "POST", f"/v1/admin/workspaces/{WORKSPACE_ID}/members"),
    (
        "admin_member_role",
        "PATCH",
        f"/v1/admin/workspaces/{WORKSPACE_ID}/members/{USER_ID}",
    ),
    ("audit_pseudonym", "POST", "/v1/admin/audit/resolve-pseudonym"),
]

# Routes that refuse an object without its two timestamps with
# "Invalid body", a refusal of its own beside the body refusal.
TIMESTAMP_ROUTES = [
    ("agent_session_group", "PUT", "/v1/agent-session-groups/grp_1"),
    ("agent_session", "PUT", "/v1/agent-sessions/ses_1"),
    ("knowledge_session_group", "PUT", "/v1/knowledge-session-groups/grp_1"),
    ("knowledge_session", "PUT", "/v1/knowledge-sessions/ses_1"),
    ("asset_section", "PUT", "/v1/assets/sections/sec_1"),
    ("asset_group", "PUT", "/v1/assets/groups/grp_1"),
    ("asset_reservation", "POST", "/v1/assets/ast_1/upload-reservation"),
    ("asset_save", "PUT", "/v1/assets/ast_1"),
    ("vector_index_save", "PUT", "/v1/vector-indexes/vix_1"),
]

# Further routes that answered a broken body with a text of their own.
OWN_TEXT_ROUTES = [
    ("account_preferences", "PUT", "/v1/account/preferences"),
    ("asset_bulk_delete", "POST", "/v1/assets/deletion-operations"),
    ("skill_import", "POST", "/v1/skills/import"),
]

# Routes whose fields are all optional, with the status each answers past
# the body check: agent memory is off in this app, the other ids are unknown.
OPTIONAL_BODY_ROUTES = [
    ("memory_clear", "POST", "/v1/agent/memory:clear", 404),
    ("memory_accept", "POST", "/v1/agent/memory/candidates/cand_1:accept", 404),
    ("memory_reject", "POST", "/v1/agent/memory/candidates/cand_1:reject", 404),
    ("patch_reject", "POST", "/v1/editor/patches/pat_1:reject", 404),
    ("vector_index_delete", "DELETE", "/v1/vector-indexes/vix_unknown", 404),
]

# Routes mounted only with an invitation store; the caller is an instance
# admin and the OWNER of the workspace the fixture creates.
WORKSPACE_ROUTES = [
    ("workspace_create", "POST", "/v1/workspaces"),
    ("workspace_invite", "POST", "/v1/workspaces/{workspace_id}/invitations"),
]

NON_OBJECT_BODIES: dict[str, Any] = {
    "array": ["x"],
    "empty_array": [],
    "string": "x",
    "true": True,
    "null": None,
}


def _route_params(routes: list[tuple[Any, ...]]) -> list[Any]:
    return [pytest.param(route[1], route[2], id=route[0]) for route in routes]


BODY_PARAMS = [
    pytest.param(body, id=name) for name, body in NON_OBJECT_BODIES.items()
]


@pytest.fixture(scope="module")
def server_client() -> Iterator[tuple[TestClient, str]]:
    # The app configures the process-wide pseudonym key at startup; the
    # module restores it so no later test inherits this test's pepper.
    saved_key = log_redaction._stable_key
    saved_warned = log_redaction._fallback_warned
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(testing_mode=True),
        server=ServerSettings(),
        storage=StorageSettings(backend="memory", database_url=""),
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            pseudonym_pepper="non-object-body-pepper",
            oidc_insecure_dev_cookies=True,
        ),
    )
    app = create_app(
        settings=settings,
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
    )
    try:
        with TestClient(app, base_url="http://127.0.0.1:5100") as client:
            setup = client.post(
                "/api/setup/owner",
                json={
                    "email": OWNER[0],
                    "password": OWNER[1],
                    "display_name": "Owner",
                },
            )
            assert setup.status_code in (200, 201), setup.text
            csrf = client.get("/api/auth/session").json()["csrf_token"]
            yield client, csrf
    finally:
        log_redaction._stable_key = saved_key
        log_redaction._fallback_warned = saved_warned


@pytest.fixture(scope="module")
def workspace_client() -> tuple[TestClient, str, str]:
    client, idp, _identity, _invitations, _users = make_workspace_world(
        registration="open"
    )
    _response, csrf = workspace_login(client, idp)
    created = client.post(
        "/v1/workspaces",
        json={"name": "Team"},
        headers={"X-CSRF-Token": csrf},
    )
    assert created.status_code == 201, created.text
    return client, csrf, created.json()["workspace_id"]


def _send(
    client: TestClient,
    method: str,
    path: str,
    csrf: str,
    content: bytes | None = None,
):
    headers = {"X-CSRF-Token": csrf}
    if content is not None:
        headers["Content-Type"] = "application/json"
    return client.request(method, path, content=content, headers=headers)


def _send_json(
    client: TestClient, method: str, path: str, csrf: str, body: Any
):
    return _send(client, method, path, csrf, json.dumps(body).encode())


def _assert_refused_like_malformed(
    client: TestClient, method: str, path: str, csrf: str, body: Any
) -> None:
    malformed = _send(client, method, path, csrf, MALFORMED)
    assert malformed.status_code == 400, malformed.text
    envelope = malformed.json()
    assert envelope == INVALID_JSON_BODY_ENVELOPE

    response = _send_json(client, method, path, csrf, body)

    assert response.status_code == 400, response.text
    assert response.json() == envelope


def _boolean_refusal(key: str) -> dict[str, Any]:
    return {
        "error": {
            "message": f"Field '{key}' must be true or false.",
            "type": "invalid_request_error",
        }
    }


# ------------------------------------------------------------------ #
# A body that is no JSON object
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("body", BODY_PARAMS)
@pytest.mark.parametrize(
    ("method", "path"),
    _route_params(
        SERVER_ROUTES + TIMESTAMP_ROUTES + OWN_TEXT_ROUTES + OPTIONAL_BODY_ROUTES
    ),
)
def test_server_route_refuses_a_non_object_body_like_a_malformed_one(
    server_client: tuple[TestClient, str], method: str, path: str, body: Any
) -> None:
    client, csrf = server_client
    _assert_refused_like_malformed(client, method, path, csrf, body)


@pytest.mark.parametrize("body", BODY_PARAMS)
@pytest.mark.parametrize(("method", "path"), _route_params(WORKSPACE_ROUTES))
def test_workspace_route_refuses_a_non_object_body_like_a_malformed_one(
    workspace_client: tuple[TestClient, str, str],
    method: str,
    path: str,
    body: Any,
) -> None:
    client, csrf, workspace_id = workspace_client
    _assert_refused_like_malformed(
        client, method, path.format(workspace_id=workspace_id), csrf, body
    )


@pytest.mark.parametrize(("method", "path"), _route_params(TIMESTAMP_ROUTES))
def test_object_without_its_timestamps_keeps_the_invalid_body_refusal(
    server_client: tuple[TestClient, str], method: str, path: str
) -> None:
    client, csrf = server_client

    response = _send_json(client, method, path, csrf, {})

    assert response.status_code == 400, response.text
    assert response.json() == {
        "error": {"message": "Invalid body", "type": "invalid_request_error"}
    }


# ------------------------------------------------------------------ #
# A body that may be left out
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        pytest.param(method, path, status, id=name)
        for name, method, path, status in OPTIONAL_BODY_ROUTES
    ],
)
def test_optional_body_route_accepts_a_request_without_a_body(
    server_client: tuple[TestClient, str], method: str, path: str, status: int
) -> None:
    client, csrf = server_client

    response = _send(client, method, path, csrf)

    assert response.status_code == status, response.text
    assert response.json() != INVALID_JSON_BODY_ENVELOPE


def test_vector_index_delete_without_a_body_starts_the_deletion(
    server_client: tuple[TestClient, str],
) -> None:
    client, csrf = server_client
    saved = _send_json(
        client,
        "PUT",
        "/v1/vector-indexes/vix_delete_1",
        csrf,
        {"model": "text-embedding-3-small", "dims": 1536,
         "created_at": 1.0, "updated_at": 1.0},
    )
    assert saved.status_code == 200, saved.text

    # The research desk sends this request without a body and without a
    # Content-Type when it has no collection hint.
    deleted = _send(client, "DELETE", "/v1/vector-indexes/vix_delete_1", csrf)

    assert deleted.status_code == 202, deleted.text


# ------------------------------------------------------------------ #
# Boolean fields
# ------------------------------------------------------------------ #


def _fake_chat_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return minimal_agent_result()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("stream", "false", id="stream_text"),
        pytest.param("stream", 0, id="stream_number"),
        pytest.param("include_progress", "yes", id="include_progress_text"),
        pytest.param("include_progress", None, id="include_progress_null"),
    ],
)
def test_chat_boolean_field_takes_only_true_or_false(
    monkeypatch: pytest.MonkeyPatch, key: str, value: Any
) -> None:
    monkeypatch.setattr(web_research_module, "run_web_graph", _fake_chat_run)
    body = {"messages": [{"role": "user", "content": "Hallo"}], key: value}

    with make_contract_client() as client:
        response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 400, response.text
    assert response.json() == _boolean_refusal(key)


def test_chat_stream_null_means_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # OpenAI's chat-completions contract lets ``stream`` be null.
    monkeypatch.setattr(web_research_module, "run_web_graph", _fake_chat_run)
    body = {"messages": [{"role": "user", "content": "Hallo"}], "stream": None}

    with make_contract_client() as client:
        response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200, response.text
    assert response.json()["object"] == "chat.completion"


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("false", id="text"),
        pytest.param("true", id="text_true"),
        pytest.param(1, id="number"),
        pytest.param(None, id="null"),
    ],
)
def test_account_preferences_memory_switch_takes_only_true_or_false(
    server_client: tuple[TestClient, str], value: Any
) -> None:
    client, csrf = server_client

    response = _send_json(
        client,
        "PUT",
        "/v1/account/preferences",
        csrf,
        {"updated_at": 10.0, "enable_agent_memory": value},
    )

    assert response.status_code == 400, response.text
    assert response.json() == _boolean_refusal("enable_agent_memory")


def test_account_preferences_memory_switch_is_off_when_left_out(
    server_client: tuple[TestClient, str],
) -> None:
    client, csrf = server_client

    switched_on = _send_json(
        client,
        "PUT",
        "/v1/account/preferences",
        csrf,
        {"updated_at": 20.0, "enable_agent_memory": True},
    )
    left_out = _send_json(
        client, "PUT", "/v1/account/preferences", csrf, {"updated_at": 21.0}
    )

    assert switched_on.status_code == 200, switched_on.text
    assert switched_on.json()["enable_agent_memory"] is True
    assert left_out.status_code == 200, left_out.text
    assert left_out.json()["enable_agent_memory"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        pytest.param(
            "POST",
            "/v1/assets/ast_bool_1/upload-reservation",
            {"section_id": "sec_bool", "size_bytes": 1,
             "created_at": 1.0, "updated_at": 1.0},
            id="upload_reservation",
        ),
        pytest.param(
            "PUT",
            "/v1/assets/ast_bool_2",
            {"section_id": "sec_bool", "created_at": 1.0, "updated_at": 1.0},
            id="asset_save",
        ),
    ],
)
def test_asset_text_truncated_takes_only_true_or_false(
    server_client: tuple[TestClient, str],
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    client, csrf = server_client

    response = _send_json(
        client, method, path, csrf, {**body, "text_truncated": "false"}
    )

    assert response.status_code == 400, response.text
    assert response.json() == _boolean_refusal("text_truncated")
