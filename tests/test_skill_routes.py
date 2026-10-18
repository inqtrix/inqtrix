"""Skill CRUD, validation, ownership wiring, and sharing.

Same world as the prompt-template route tests (full oidc container,
memory backends). The owned-resource GRADE semantics (view/edit/owner)
are pinned in tests/test_prompt_template_routes.py — here we pin what
is SKILL-specific: the field validation matrix (label shape, enums,
clarification-point sanitizer, the placeholder coupling rule), the
deterministic point ids, and that the skill resource type is wired into
the share layer at all.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.server.routers.skills import build_router as build_skills_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubLLM, StubSearch
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    OidcHeaderProvider,
    user_headers,
)

PAYLOAD = {
    "label": "sprechzettel",
    "title": "Sprechzettel",
    "description": "Kompakter Sprechzettel fuer Termine.",
    "when_to_use": "Wenn der Nutzer Stichpunkte fuer einen Termin braucht.",
    "instructions_markdown": (
        "Erstelle einen Sprechzettel fuer {{anlass}} mit Blick auf "
        "{{publikum}}."
    ),
    "clarification_points": [
        {
            "name": "anlass",
            "question": "Fuer welchen Anlass ist der Sprechzettel?",
            "options": [
                {"label": "Vorstandssitzung"},
                {"label": "Kundentermin", "description": "Extern"},
            ],
            "required": True,
            "default_assumption": "Interner Termin",
        },
        {
            "name": "publikum",
            "question": "Wer ist das Publikum?",
            "options": [],
            "required": False,
        },
    ],
    "deliverable": "talking_points",
    "allowed_tools": ["search_project_knowledge", "web_instant"],
    "requires_plan": "never",
    "invocation": "model_allowed",
    "argument_hint": "Anlass und Kernbotschaft",
    "model_tier": "mid",
    "effort": "low",
    "include_in_autocomplete": True,
}


def make_world():
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror() -> None:
        for user_id, subject, name in (
            (OWNER, "user-owner", "Olga Owner"),
            (RECIPIENT, "user-recipient", "Rita Recipient"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=name,
                canonical_user_id=user_id,
            )

    asyncio.run(mirror())
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=OidcHeaderProvider(users),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    assert container.skill_service is not None
    assert "skill_template" in container.share_service.resource_types
    app = FastAPI()
    app.include_router(build_skills_router(container))
    app.include_router(build_shares_router(container))
    return TestClient(app), container


@pytest.fixture()
def world():
    return make_world()


def as_user(user_id: uuid.UUID) -> dict[str, str]:
    return user_headers(user_id)


def create_skill(
    client: TestClient, *, user_id: uuid.UUID = OWNER, **overrides
) -> dict:
    response = client.post(
        "/v1/skills",
        json={**PAYLOAD, **overrides},
        headers=as_user(user_id),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_crud_roundtrip_with_sanitized_points(world):
    client, _container = world
    created = create_skill(client)
    assert created["access"] == {"mode": "owner"}
    assert created["label"] == "sprechzettel"
    assert created["deliverable"] == "talking_points"
    assert created["allowed_tools"] == [
        "search_project_knowledge",
        "web_instant",
    ]
    # Deterministic positional ids, never client-minted.
    points = created["clarification_points"]
    assert [point["id"] for point in points] == ["p1", "p2"]
    assert [option["id"] for option in points[0]["options"]] == [
        "p1_o1",
        "p1_o2",
    ]
    assert points[0]["required"] is True
    assert points[1]["options"] == []

    listed = client.get("/v1/skills", headers=as_user(OWNER)).json()["data"]
    assert [item["id"] for item in listed] == [created["id"]]

    updated = client.put(
        f"/v1/skills/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Sprechzettel v2",
            "expected_revision": created["revision"],
        },
        headers=as_user(OWNER),
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Sprechzettel v2"

    deleted = client.delete(
        f"/v1/skills/{created['id']}", headers=as_user(OWNER)
    )
    assert deleted.status_code == 204
    assert client.get("/v1/skills", headers=as_user(OWNER)).json()["data"] == []


def test_validation_matrix(world):
    client, _container = world

    def rejected(**overrides) -> str:
        response = client.post(
            "/v1/skills", json={**PAYLOAD, **overrides}, headers=as_user(OWNER)
        )
        assert response.status_code == 400, response.text
        return response.json()["error"]["message"]

    assert "label" in rejected(label="Kein Label!")
    assert "title" in rejected(title="")
    assert "instructions_markdown" in rejected(instructions_markdown="  ")
    assert "deliverable" in rejected(deliverable="poster")
    assert "requires_plan" in rejected(requires_plan="maybe")
    assert "invocation" in rejected(invocation="anyone")
    assert "model_tier" in rejected(model_tier="ultra")
    assert "effort" in rejected(effort="max")
    assert "allowed_tools" in rejected(allowed_tools=["ok", ""])
    assert "clarification points" in rejected(
        clarification_points=[
            {"name": f"p{i}", "question": "Q"} for i in range(6)
        ]
    )
    assert "options are allowed" in rejected(
        clarification_points=[
            {
                "name": "anlass",
                "question": "Q",
                "options": [{"label": str(i)} for i in range(5)],
            },
            {"name": "publikum", "question": "Q"},
        ]
    )
    assert "requires a question" in rejected(
        clarification_points=[
            {"name": "anlass", "question": " "},
            {"name": "publikum", "question": "Q"},
        ]
    )
    # Over-long text is REFUSED, never silently shortened: a 200 for a
    # field the server then throws away is the worst of both worlds --
    # the author believes it was saved and the full text is gone.
    assert "at most 500 characters" in rejected(
        clarification_points=[
            {"name": "anlass", "question": "Q" * 501},
        ]
    )
    assert "at most 300 characters" in rejected(
        clarification_points=[
            {
                "name": "anlass",
                "question": "Q",
                "default_assumption": "A" * 301,
            },
        ]
    )
    assert "at most 60 characters" in rejected(
        clarification_points=[
            {
                "name": "anlass",
                "question": "Q",
                "options": [{"label": "L" * 61}],
            },
        ]
    )
    assert "at most 120 characters" in rejected(
        clarification_points=[
            {
                "name": "anlass",
                "question": "Q",
                "options": [{"label": "L", "description": "D" * 121}],
            },
        ]
    )
    assert "at most 120 characters" in rejected(argument_hint="H" * 121)


def test_placeholder_coupling_rule(world):
    client, _container = world
    # A {{placeholder}} without a matching point is a loud 400 — the
    # substitution would otherwise leave a hole mid-run.
    response = client.post(
        "/v1/skills",
        json={
            **PAYLOAD,
            "instructions_markdown": "Schreibe fuer {{anlass}} und {{ton}}.",
        },
        headers=as_user(OWNER),
    )
    assert response.status_code == 400
    assert "ton" in response.json()["error"]["message"]

    # Points WITHOUT a placeholder stay allowed (context-only inputs).
    created = create_skill(
        client,
        instructions_markdown="Erstelle den Sprechzettel fuer {{anlass}}.",
        clarification_points=[
            *PAYLOAD["clarification_points"],
            {"question": "Gibt es Sperrfristen?", "required": False},
        ],
    )
    assert len(created["clarification_points"]) == 3
    assert created["clarification_points"][2]["name"] == ""


def test_stranger_is_blind_and_share_wiring_works(world):
    client, _container = world
    created = create_skill(client)
    skill_id = created["id"]

    # Stranger: indistinct absence, reads and writes alike.
    assert client.get("/v1/skills", headers=as_user(RECIPIENT)).json()[
        "data"
    ] == []
    assert (
        client.put(
            f"/v1/skills/{skill_id}",
            json={**PAYLOAD, "expected_revision": created["revision"]},
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )

    # View grant (accepted) admits reads, not writes — proves the skill
    # resource type is wired through the share layer end to end.
    granted = client.post(
        "/v1/shares",
        json={
            "resource_type": "skill_template",
            "resource_id": skill_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": "view"}
            ],
        },
        headers=as_user(OWNER),
    )
    assert granted.status_code == 201, granted.text
    share_id = granted.json()["data"][0]["id"]
    accepted = client.post(
        f"/v1/shares/{share_id}/accept", headers=as_user(RECIPIENT)
    )
    assert accepted.status_code == 200

    visible = client.get("/v1/skills", headers=as_user(RECIPIENT)).json()[
        "data"
    ]
    assert [item["id"] for item in visible] == [skill_id]
    assert visible[0]["access"] == {
        "mode": "shared",
        "permission": "view",
    }
    assert (
        client.put(
            f"/v1/skills/{skill_id}",
            json={**PAYLOAD, "expected_revision": created["revision"]},
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/v1/skills/{skill_id}", headers=as_user(RECIPIENT)
        ).status_code
        == 404
    )


def test_stale_precondition_is_409(world):
    client, _container = world
    created = create_skill(client)
    response = client.put(
        f"/v1/skills/{created['id']}",
        json={**PAYLOAD, "expected_revision": created["revision"] + 1},
        headers=as_user(OWNER),
    )
    assert response.status_code == 409
    assert response.json()["error"]["current_revision"] == created["revision"]
    fresh = client.put(
        f"/v1/skills/{created['id']}",
        json={**PAYLOAD, "expected_revision": created["revision"]},
        headers=as_user(OWNER),
    )
    assert fresh.status_code == 200

def test_export_import_roundtrip_over_http(world):
    client, _container = world
    created = create_skill(client)

    exported = client.get(
        f"/v1/skills/{created['id']}/markdown", headers=as_user(OWNER)
    )
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/markdown")
    text = exported.text
    assert text.startswith("---\n")
    assert "name: sprechzettel" in text
    assert "x-inqtrix:" in text
    assert "{{anlass}}" in text

    # A stranger cannot export what they cannot see.
    assert (
        client.get(
            f"/v1/skills/{created['id']}/markdown",
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )

    reimported = client.post(
        "/v1/skills/import", json={"markdown": text}, headers=as_user(OWNER)
    )
    assert reimported.status_code == 201, reimported.text
    body = reimported.json()
    assert body["id"] != created["id"]
    for field in (
        "label",
        "title",
        "description",
        "when_to_use",
        "instructions_markdown",
        "clarification_points",
        "deliverable",
        "allowed_tools",
        "requires_plan",
        "invocation",
        "argument_hint",
        "model_tier",
        "effort",
        "include_in_autocomplete",
    ):
        assert body[field] == created[field], field


def test_import_rejections_stay_loud(world):
    client, _container = world

    # Missing/typed-wrong body field.
    response = client.post(
        "/v1/skills/import", json={"markdown": 7}, headers=as_user(OWNER)
    )
    assert response.status_code == 400
    assert "markdown" in response.json()["error"]["message"]

    # File-shape failure (parser error, mapped to 400).
    response = client.post(
        "/v1/skills/import",
        json={"markdown": "kein frontmatter"},
        headers=as_user(OWNER),
    )
    assert response.status_code == 400
    assert "frontmatter block" in response.json()["error"]["message"]

    # Policy failure: the SERVICE validator is the gate — a file whose
    # body has a placeholder without a matching point must not slip in
    # through the import side door.
    broken = (
        "---\n"
        "name: kaputt\n"
        "description: Testskill\n"
        "---\n\n"
        "Schreibe fuer {{ton}}.\n"
    )
    response = client.post(
        "/v1/skills/import", json={"markdown": broken}, headers=as_user(OWNER)
    )
    assert response.status_code == 400
    assert "ton" in response.json()["error"]["message"]
