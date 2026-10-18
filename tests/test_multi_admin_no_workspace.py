"""Acceptance contract for workspace-free multi-admin installations.

The reported sync failure appeared after adding a second instance admin while
quotas were disabled. This test drives the public application factory with
three independent cookie sessions. It pins the actual contract: instance-admin
status does not create workspace membership, quota stays absent, pagination is
not quota, and every per-user sync surface remains available and isolated.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.providers.base import ProviderContext
from inqtrix.server.app import create_app
from inqtrix.settings import (
    AuthSettings,
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import StubLLM, StubSearch

_PASSWORD = "correct-horse-battery"
_PREFERENCES = (
    ("dark", "slate", "mint"),
    ("light", "petrol", "orange"),
    ("system", "graphite", "sky"),
)


def _settings() -> Settings:
    return Settings(
        auth=AuthSettings(
            mode="local",
            session_secret="s" * 32,
            pat_pepper="p" * 32,
            oidc_insecure_dev_cookies=True,
        ),
        quota=QuotaSettings(enabled=False),
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
    )


def _login(app: FastAPI, email: str) -> TestClient:
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    response = client.post(
        "/api/auth/login/local",
        json={"email": email, "password": _PASSWORD},
    )
    assert response.status_code == 200, response.text
    return client


def test_three_admins_without_workspaces_sync_with_quota_disabled() -> None:
    app = create_app(
        settings=_settings(),
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
    )
    owner = TestClient(app, base_url="http://127.0.0.1:5100")
    setup = owner.post(
        "/api/setup/owner",
        json={
            "email": "admin-1@example.com",
            "password": _PASSWORD,
            "display_name": "Admin One",
        },
    )
    assert setup.status_code == 201, setup.text
    owner_session = owner.get("/api/auth/session").json()
    owner_csrf = owner_session["csrf_token"]

    for index in (2, 3):
        created = owner.post(
            "/v1/admin/users",
            json={
                "email": f"admin-{index}@example.com",
                "password": _PASSWORD,
                "instance_role": "admin",
            },
            headers={"X-CSRF-Token": owner_csrf},
        )
        assert created.status_code == 201, created.text
        assert created.json()["instance_role"] == "admin"

    assert app.state.container.quota_service is None

    clients = [owner]
    clients.extend(
        _login(app, f"admin-{index}@example.com") for index in (2, 3)
    )
    section_ids: list[str] = []
    session_ids: list[str] = []

    for index, (client, preference_values) in enumerate(
        zip(clients, _PREFERENCES, strict=True),
        start=1,
    ):
        auth_session = client.get("/api/auth/session").json()
        assert auth_session["user"]["role"] == "admin"
        csrf = auth_session["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        workspaces = client.get("/v1/admin/workspaces")
        assert workspaces.status_code == 200
        assert workspaces.json()["data"] == []

        theme, preset, bubble = preference_values
        saved_preferences = client.put(
            "/v1/account/preferences",
            json={
                "contrast_mode": "standard",
                "locale": "en",
                "theme": theme,
                "theme_preset": preset,
                "user_bubble_tone": bubble,
                "enable_agent_memory": index == 3,
                "updated_at": float(index),
            },
            headers=headers,
        )
        assert saved_preferences.status_code == 200, saved_preferences.text
        assert client.get("/v1/account/preferences").json()["updated_at"] == float(
            index
        )

        runs = client.get("/v1/runs?limit=100")
        assert runs.status_code == 200, runs.text
        assert runs.json()["data"] == []
        skills = client.get("/v1/skills")
        assert skills.status_code == 200, skills.text
        assert skills.json()["data"] == []

        section_id = f"file-section-{uuid.uuid4()}"
        section_ids.append(section_id)
        section = client.put(
            f"/v1/assets/sections/{section_id}",
            json={
                "kind": "temporary",
                "title": f"Admin {index}",
                "created_at": float(index),
                "updated_at": float(index),
            },
            headers=headers,
        )
        assert section.status_code == 200, section.text

        knowledge_session_id = f"knowledge-session-{uuid.uuid4()}"
        session_ids.append(knowledge_session_id)
        knowledge_session = client.put(
            f"/v1/knowledge-sessions/{knowledge_session_id}",
            json={
                "title": f"Admin {index}",
                "items_json": "[]",
                "group_id": None,
                "created_at": float(index),
                "updated_at": float(index),
            },
            headers=headers,
        )
        assert knowledge_session.status_code == 200, knowledge_session.text

        assert client.get("/v1/stacks").status_code == 404
        missing_delete = client.delete(
            f"/v1/assets/sections/file-section-missing-{index}",
            headers=headers,
        )
        assert missing_delete.status_code == 404

    for index, client in enumerate(clients):
        sections = client.get("/v1/assets/sections").json()["data"]
        sessions = client.get("/v1/knowledge-sessions").json()["data"]
        assert [row["id"] for row in sections] == [section_ids[index]]
        assert [row["id"] for row in sessions] == [session_ids[index]]
