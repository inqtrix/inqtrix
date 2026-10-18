"""Prompt-template CRUD, ownership, and sharing.

Same world as the knowledge-ownership matrix: the full oidc container
with memory backends. Pins the CRUD contract, the owned-resource
rule (owner full, stranger 404, ownerless open), the share grades
(view reads, edit updates, owner-only deletion), share revocation on
deletion, and the validation envelope.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.content.prompt_templates import (
    PromptTemplateRecord,
    new_template_id,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.prompt_templates import (
    build_router as build_templates_router,
)
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubLLM, StubSearch
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    OidcHeaderProvider,
    user_headers,
)

PAYLOAD = {
    "title": "Executive Briefing",
    "label": "briefing",
    "category": "instruction",
    "content_markdown": "Fasse die Lage in drei Saetzen zusammen.",
    "visibility": {"chat": True, "editor": False},
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
    assert container.prompt_template_service is not None
    assert "prompt_template" in container.share_service.resource_types
    app = FastAPI()
    app.include_router(build_templates_router(container))
    app.include_router(build_shares_router(container))
    return TestClient(app), container


@pytest.fixture()
def world():
    return make_world()


def as_user(user_id: uuid.UUID) -> dict[str, str]:
    return user_headers(user_id)


def create_template(
    client: TestClient, *, user_id: uuid.UUID = OWNER
) -> dict:
    response = client.post(
        "/v1/prompt-templates", json=PAYLOAD, headers=as_user(user_id)
    )
    assert response.status_code == 201
    return response.json()


def grant(client: TestClient, template_id: str, *, permission: str = "view"):
    response = client.post(
        "/v1/shares",
        json={
            "resource_type": "prompt_template",
            "resource_id": template_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": permission}
            ],
        },
        headers=as_user(OWNER),
    )
    # These tests assert post-acceptance access, so the recipient consents
    # here. The pending/idempotent-consent lifecycle itself is pinned in the
    # dedicated share tests.
    if response.status_code == 201:
        share_id = response.json()["data"][0]["id"]
        accepted = client.post(
            f"/v1/shares/{share_id}/accept", headers=as_user(RECIPIENT)
        )
        assert accepted.status_code == 200
    return response


def seed_ownerless(container) -> str:
    """An ownerless template, as anonymous/static principals create."""
    now = time.time()
    record = PromptTemplateRecord(
        id=new_template_id(),
        tenant_id="default",
        owner_user_id=None,
        title="Bestand",
        label="bestand",
        category=None,
        content_markdown="Alte lokale Regel.",
        created_at=now,
        updated_at=now,
    )
    asyncio.run(
        container.prompt_template_service._repository.create(record)
    )
    return record.id


def test_crud_roundtrip_for_the_owner(world):
    client, _container = world
    created = create_template(client)
    assert created["title"] == PAYLOAD["title"]
    assert created["access"] == {"mode": "owner"}

    listed = client.get(
        "/v1/prompt-templates", headers=as_user(OWNER)
    ).json()["data"]
    assert [item["id"] for item in listed] == [created["id"]]

    updated = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Neuer Titel",
            "expected_revision": created["revision"],
        },
        headers=as_user(OWNER),
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Neuer Titel"
    assert updated.json()["updated_at"] >= created["updated_at"]

    deleted = client.delete(
        f"/v1/prompt-templates/{created['id']}", headers=as_user(OWNER)
    )
    assert deleted.status_code == 204
    assert (
        client.get("/v1/prompt-templates", headers=as_user(OWNER)).json()[
            "data"
        ]
        == []
    )


def test_every_template_publishes_its_composed_size(world):
    """The agent composer pre-checks attached rules against the composed
    ceiling. It must not re-implement the composition (labels fold,
    bodies strip, markers are defused, and JavaScript counts UTF-16
    units), so each template carries the size of the block it composes
    into — on create, list and update alike."""
    client, _container = world
    created = client.post(
        "/v1/prompt-templates",
        json={
            **PAYLOAD,
            "label": "Vorstand\n  [kurz]",
            "content_markdown": "  Fasse die Lage zusammen.  ",
        },
        headers=as_user(OWNER),
    )
    assert created.status_code == 201, created.text
    expected = len(
        "[Rule: Vorstand (kurz)]\n"
        "Fasse die Lage zusammen.\n"
        "[End rule: Vorstand (kurz)]"
    )
    assert created.json()["composed_chars"] == expected
    listed = client.get(
        "/v1/prompt-templates", headers=as_user(OWNER)
    ).json()["data"]
    assert [item["composed_chars"] for item in listed] == [expected]
    updated = client.put(
        f"/v1/prompt-templates/{created.json()['id']}",
        json={
            **PAYLOAD,
            "label": "Vorstand",
            "content_markdown": "Kurz.",
            "expected_revision": created.json()["revision"],
        },
        headers=as_user(OWNER),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["composed_chars"] == len(
        "[Rule: Vorstand]\nKurz.\n[End rule: Vorstand]"
    )


def test_stranger_is_blind_and_cannot_write(world):
    client, _container = world
    created = create_template(client)

    stranger_list = client.get(
        "/v1/prompt-templates", headers=as_user(RECIPIENT)
    ).json()["data"]
    # The list carries only the stranger's OWN rows (their lazily
    # seeded stock prompts) — never the owner's template.
    assert created["id"] not in [item["id"] for item in stranger_list]
    assert all(
        item["access"] == {"mode": "owner"} for item in stranger_list
    )
    denied_update = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={**PAYLOAD, "expected_revision": created["revision"]},
        headers=as_user(RECIPIENT),
    )
    assert denied_update.status_code == 404
    denied_delete = client.delete(
        f"/v1/prompt-templates/{created['id']}", headers=as_user(RECIPIENT)
    )
    assert denied_delete.status_code == 404
    missing = client.delete(
        "/v1/prompt-templates/pt_does_not_exist", headers=as_user(RECIPIENT)
    )
    assert denied_delete.json() == missing.json()


def test_ownerless_templates_stay_open(world):
    client, container = world
    template_id = seed_ownerless(container)
    for user_id in (OWNER, RECIPIENT):
        visible = client.get(
            "/v1/prompt-templates", headers=as_user(user_id)
        ).json()["data"]
        # Scoped users never see the ownerless row; what they DO see
        # is exclusively their own (seeded) templates.
        assert template_id not in [item["id"] for item in visible]
        assert all(
            item["access"] == {"mode": "owner"} for item in visible
        )
    listed = client.get("/v1/prompt-templates").json()["data"]
    assert [item["id"] for item in listed] == [template_id]
    assert listed[0]["access"] == {"mode": "unscoped"}
    # Ownerless legacy templates stay writable only in legacy unscoped mode.
    assert (
        client.put(
            f"/v1/prompt-templates/{template_id}",
            json={**PAYLOAD, "expected_revision": 1},
        ).status_code
        == 200
    )
    # And unshareable: no owner means no grant authority.
    assert grant(client, template_id).status_code == 404


def test_view_grant_admits_reads_not_writes(world):
    client, _container = world
    created = create_template(client)
    assert grant(client, created["id"]).status_code == 201

    listed = client.get(
        "/v1/prompt-templates", headers=as_user(RECIPIENT)
    ).json()["data"]
    shared_rows = [item for item in listed if item["id"] == created["id"]]
    assert len(shared_rows) == 1
    assert shared_rows[0]["access"] == {
        "mode": "shared", "permission": "view",
    }

    assert (
        client.put(
            f"/v1/prompt-templates/{created['id']}",
            json={**PAYLOAD, "expected_revision": created["revision"]},
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )


def test_edit_grant_admits_updates_not_deletion(world):
    client, _container = world
    created = create_template(client)
    assert grant(client, created["id"], permission="edit").status_code == 201

    updated = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Von Rita angepasst",
            "expected_revision": created["revision"],
        },
        headers=as_user(RECIPIENT),
    )
    assert updated.status_code == 200
    # Last write wins: the owner sees the recipient's edit.
    owner_view = client.get(
        "/v1/prompt-templates", headers=as_user(OWNER)
    ).json()["data"]
    assert owner_view[0]["title"] == "Von Rita angepasst"

    assert (
        client.delete(
            f"/v1/prompt-templates/{created['id']}",
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )


def test_deletion_revokes_shares(world):
    client, _container = world
    created = create_template(client)
    assert grant(client, created["id"]).status_code == 201
    assert (
        client.delete(
            f"/v1/prompt-templates/{created['id']}", headers=as_user(OWNER)
        ).status_code
        == 204
    )
    inbox = client.get(
        "/v1/shares/inbox", headers=as_user(RECIPIENT)
    ).json()["data"]
    assert inbox == {"pending": [], "accepted": []}


def test_validation_envelope(world):
    client, _container = world
    missing_title = client.post(
        "/v1/prompt-templates",
        json={**PAYLOAD, "title": " "},
        headers=as_user(OWNER),
    )
    assert missing_title.status_code == 400
    assert "title" in missing_title.json()["error"]["message"]
    bad_category = client.post(
        "/v1/prompt-templates",
        json={**PAYLOAD, "category": "comet"},
        headers=as_user(OWNER),
    )
    assert bad_category.status_code == 400


# ---------------------------------------------------------------------------
# Mandatory integer revision (stale-write protection)
# ---------------------------------------------------------------------------


def test_update_with_matching_precondition_succeeds(world):
    client, _container = world
    created = create_template(client)
    updated = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Frisch",
            "expected_revision": created["revision"],
        },
        headers=as_user(OWNER),
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Frisch"
    assert updated.json()["revision"] == created["revision"] + 1
    assert updated.json()["updated_at"] >= created["updated_at"]


def test_stale_precondition_is_409_then_recoverable(world):
    client, _container = world
    created = create_template(client)
    stale_revision = created["revision"]

    # A first writer advances the version.
    first = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Von A",
            "expected_revision": stale_revision,
        },
        headers=as_user(OWNER),
    )
    assert first.status_code == 200
    fresh_revision = first.json()["revision"]

    # A second writer who only saw the original version is rejected,
    # NOT silently overwriting A's edit.
    conflict = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Von B",
            "expected_revision": stale_revision,
        },
        headers=as_user(OWNER),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["current_revision"] == fresh_revision
    assert "changed in the meantime" in conflict.json()["error"]["message"]
    # A's edit survived the rejected write.
    assert (
        client.get("/v1/prompt-templates", headers=as_user(OWNER)).json()[
            "data"
        ][0]["title"]
        == "Von A"
    )

    # Re-reading the fresh anchor lets B's retry land.
    retry = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={
            **PAYLOAD,
            "title": "Von B",
            "expected_revision": fresh_revision,
        },
        headers=as_user(OWNER),
    )
    assert retry.status_code == 200
    assert retry.json()["title"] == "Von B"


def test_update_requires_revision(world):
    client, _container = world
    created = create_template(client)
    response = client.put(
        f"/v1/prompt-templates/{created['id']}",
        json={**PAYLOAD, "title": "Ohne Version"},
        headers=as_user(OWNER),
    )
    assert response.status_code == 400


def test_precondition_on_missing_template_is_404_not_409(world):
    client, _container = world
    missing = client.put(
        "/v1/prompt-templates/pt_does_not_exist",
        json={**PAYLOAD, "expected_revision": 1},
        headers=as_user(OWNER),
    )
    assert missing.status_code == 404


def test_invalid_revision_is_400(world):
    client, _container = world
    created = create_template(client)
    for bad_value in ("gestern", True, False, 0, 1.5):
        bad = client.put(
            f"/v1/prompt-templates/{created['id']}",
            json={**PAYLOAD, "expected_revision": bad_value},
            headers=as_user(OWNER),
        )
        assert bad.status_code == 400, bad_value
