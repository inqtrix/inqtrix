"""Unscoped knowledge asks pin retrieval to the caller-visible set.

The complement to ``test_knowledge_ownership``: those tests pin the
EXPLICIT ask gate (a named invisible collection denies the whole
request). Here we pin the UNSCOPED gate — an omitted, ``null``, or
empty ``collection_ids`` filter. Before the fix such an ask reached the
``mode=knowledge`` algorithm as ``None`` and :func:`retrieve` ranged
over EVERY collection in the tenant, so an authenticated stranger could
pull chunks from private collections into their answer. The admission
now resolves the scope to the caller-visible set
(:meth:`KnowledgeService.resolve_ask_scope`) on both ask surfaces
(chat + native runs).

The world is the oidc container over the memory knowledge store, with
an owner and a stranger. The leak is asserted end-to-end: the owner's
private document must never surface in the stranger's native-run
references, and its text must never reach the stranger's chat answer
prompt. ``visible_to=None`` (auth off) keeps the historical
see-everything view — pinned in the anonymous world at the bottom.
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
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers import chat, knowledge, runs, sources
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch, wait_for_run_status
from tests.test_knowledge_routes import (
    KnowledgeStubLLM,
    StubEmbeddings,
    _create_collection_with_document,
    make_knowledge_client,
)
from tests.test_runs_sharing import OWNER, RECIPIENT, SUB_HEADER, OidcHeaderProvider

# Distinctive so its presence in an answer prompt or a reference url is
# unambiguous evidence that the owner's private chunk was retrieved.
OWNER_SECRET = "GEHEIMNIS_OWNER Frist betraegt 24 Stunden."
RECIPIENT_TEXT = "Eigene Frist betraegt 48 Stunden."

# The three shapes that all mean "no explicit scope" — every one must
# pin to the caller-visible set, none may fall through to "all".
UNSCOPED_FILTERS = [
    pytest.param(None, id="filters-absent"),
    pytest.param({"collection_ids": None}, id="collection-ids-null"),
    pytest.param({"collection_ids": []}, id="collection-ids-empty"),
]


def make_ask_world() -> tuple[TestClient, KnowledgeStubLLM, MemoryKnowledgeStore]:
    """The oidc container plus the answer LLM (captures prompts) and store."""
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror() -> None:
        for user_id, name in (
            (OWNER, "Olga Owner"),
            (RECIPIENT, "Rita Recipient"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=str(user_id),
                email=f"{user_id}@example.com",
                email_verified=True,
                display_name=name,
                canonical_user_id=user_id,
            )

    asyncio.run(mirror())
    llm = KnowledgeStubLLM()
    store = MemoryKnowledgeStore()
    container = build_container(
        providers=ProviderContext(llm=llm, search=StubSearch()),
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
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(selectable=["stub-embed-legacy"]),
            store=store,
            default_top_k=4,
        ),
    )
    app = FastAPI()
    app.include_router(knowledge.build_router(container))
    app.include_router(sources.build_router(container))
    app.include_router(runs.build_router(container))
    app.include_router(chat.build_router(container))
    app.include_router(build_shares_router(container))
    # The run store is the only place a submitted request's resolved
    # filters stay observable; the routers close over the container, so
    # tests reach it through the app instead of a wider return tuple.
    app.state.container = container
    return TestClient(app), llm, store


def as_user(user_id: uuid.UUID) -> dict[str, str]:
    return {SUB_HEADER: str(user_id)}


def make_collection(
    client: TestClient,
    *,
    sub: uuid.UUID,
    name: str,
    embedding_model: str | None = None,
) -> str:
    payload = {"name": name}
    if embedding_model is not None:
        payload["embedding_model"] = embedding_model
    response = client.post(
        "/v1/knowledge/collections", json=payload, headers=as_user(sub)
    )
    assert response.status_code == 201
    return response.json()["id"]


def add_doc(
    client: TestClient,
    collection_id: str,
    *,
    sub: uuid.UUID,
    text: str,
) -> str:
    response = client.post(
        f"/v1/knowledge/collections/{collection_id}/documents",
        json={"title": "Notiz", "text": text},
        headers=as_user(sub),
    )
    assert response.status_code == 201
    return response.json()["id"]


def accept_view_grant(client: TestClient, collection_id: str) -> None:
    granted = client.post(
        "/v1/shares",
        json={
            "resource_type": "knowledge_collection",
            "resource_id": collection_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": "view"}
            ],
        },
        headers=as_user(OWNER),
    )
    assert granted.status_code == 201
    share_id = granted.json()["data"][0]["id"]
    accepted = client.post(
        f"/v1/shares/{share_id}/accept", headers=as_user(RECIPIENT)
    )
    assert accepted.status_code == 200


def run_knowledge_ask(
    client: TestClient, *, sub: uuid.UUID, filters: dict | None
) -> dict:
    body: dict = {"question": "Wie lange ist die Frist?", "mode": "knowledge"}
    if filters is not None:
        body["knowledge_filters"] = filters
    created = client.post("/v1/runs", json=body, headers=as_user(sub))
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        summary = client.get(
            f"/v1/runs/{run_id}", headers=as_user(sub)
        ).json()
        if summary.get("status") == "completed":
            break
        time.sleep(0.01)
    else:
        raise AssertionError(
            f"run {run_id} did not complete; last summary: {summary}"
        )
    result = client.get(f"/v1/runs/{run_id}/result", headers=as_user(sub))
    assert result.status_code == 200
    return result.json()


def _submitted_filters(client: TestClient, *, sub: uuid.UUID, body: dict) -> dict:
    """Submit a run and read back its PERSISTED knowledge filters."""
    created = client.post("/v1/runs", json=body, headers=as_user(sub))
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    deadline = time.time() + 2.0
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}", headers=as_user(sub)).json()
        if summary.get("status") == "completed":
            break
        time.sleep(0.01)
    # The persisted re-execution payload is what a resume replays, so it
    # is the honest place to read the admitted scope from.
    store = client.app.state.container.run_store
    record = store._records[run_id]
    payload = record.request_payload or {}
    return (payload.get("body") or {}).get("knowledge_filters") or {}


@pytest.mark.parametrize("filters", UNSCOPED_FILTERS)
def test_submission_records_whether_the_scope_was_the_users_pick(filters):
    """The pin resolves both cases to a concrete id list, so the
    list alone cannot say whose choice it was. The admission records the
    provenance explicitly — the balanced knowledge gate asks about
    exactly that difference."""
    client, _llm, _store = make_ask_world()
    with client:
        cid = make_collection(client, sub=RECIPIENT, name="Meins")
        add_doc(client, cid, sub=RECIPIENT, text=RECIPIENT_TEXT)

        body: dict = {"question": "Wie lange ist die Frist?", "mode": "knowledge"}
        if filters is not None:
            body["knowledge_filters"] = filters
        auto = _submitted_filters(client, sub=RECIPIENT, body=body)

        picked = _submitted_filters(
            client,
            sub=RECIPIENT,
            body={
                "question": "Wie lange ist die Frist?",
                "mode": "knowledge",
                "knowledge_filters": {"collection_ids": [cid]},
            },
        )

    # Both end up pinned to the same visible set ...
    assert auto.get("collection_ids") == [cid]
    assert picked.get("collection_ids") == [cid]
    # ... and only the provenance tells them apart.
    assert auto.get("explicit") is False
    assert picked.get("explicit") is True


def test_scope_all_pins_exactly_what_the_caller_may_see():
    """The session switch (`scope: "all"`) is resolved per caller: own
    collections plus ACCEPTED shares — never a stranger's private
    collection, never an invitation that was not accepted, and never the
    recipient's collections for the owner. The pin is recorded as the
    user's explicit choice."""
    client, _llm, _store = make_ask_world()
    with client:
        owner_private = make_collection(client, sub=OWNER, name="Privat")
        add_doc(client, owner_private, sub=OWNER, text=OWNER_SECRET)
        owner_shared = make_collection(client, sub=OWNER, name="Geteilt")
        add_doc(client, owner_shared, sub=OWNER, text=OWNER_SECRET)
        accept_view_grant(client, owner_shared)
        owner_pending = make_collection(client, sub=OWNER, name="Angeboten")
        offered = client.post(
            "/v1/shares",
            json={
                "resource_type": "knowledge_collection",
                "resource_id": owner_pending,
                "invitees": [{"user_id": str(RECIPIENT), "permission": "view"}],
            },
            headers=as_user(OWNER),
        )
        assert offered.status_code == 201
        mine = make_collection(client, sub=RECIPIENT, name="Meins")
        add_doc(client, mine, sub=RECIPIENT, text=RECIPIENT_TEXT)

        switch = {"question": "Wie lange ist die Frist?", "mode": "knowledge",
                  "knowledge_filters": {"scope": "all"}}
        recipient_pin = _submitted_filters(client, sub=RECIPIENT, body=switch)
        owner_pin = _submitted_filters(client, sub=OWNER, body=switch)
        answered = run_knowledge_ask(client, sub=RECIPIENT, filters={"scope": "all"})

    assert sorted(recipient_pin["collection_ids"]) == sorted([mine, owner_shared])
    assert recipient_pin["explicit"] is True
    assert recipient_pin["scope"] == "all"
    assert sorted(owner_pin["collection_ids"]) == sorted(
        [owner_private, owner_shared, owner_pending]
    )
    urls = [ref["url"] for ref in answered.get("references", [])]
    assert all(owner_private not in url for url in urls), urls
    assert all(owner_pending not in url for url in urls), urls


def test_scope_and_chips_together_pin_the_chips_and_say_so():
    """A request carrying both keys is pinned to its chips; the persisted
    filters must not keep claiming the whole boundary."""
    client, _llm, _store = make_ask_world()
    with client:
        cid = make_collection(client, sub=RECIPIENT, name="Meins")
        add_doc(client, cid, sub=RECIPIENT, text=RECIPIENT_TEXT)
        pinned = _submitted_filters(client, sub=RECIPIENT, body={
            "question": "Wie lange ist die Frist?", "mode": "knowledge",
            "knowledge_filters": {"scope": "all", "collection_ids": [cid]},
        })
    assert pinned["collection_ids"] == [cid]
    assert pinned["explicit"] is True
    assert "scope" not in pinned


def test_chat_completions_refuse_an_unknown_scope_like_native_runs():
    client, _llm, _store = make_ask_world()
    with client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Wie lange ist die Frist?"}],
                "mode": "knowledge",
                "knowledge_filters": {"scope": "everything"},
            },
            headers=as_user(RECIPIENT),
        )
    assert response.status_code == 400
    assert "scope" in response.text


@pytest.mark.parametrize("filters", UNSCOPED_FILTERS)
def test_unscoped_native_run_excludes_foreign_documents(filters):
    """A stranger's unscoped native run never cites the owner's private
    document — the scope is pinned to what the stranger may see."""
    client, _llm, _store = make_ask_world()
    with client:
        owner_cid = make_collection(client, sub=OWNER, name="Privat")
        owner_doc = add_doc(client, owner_cid, sub=OWNER, text=OWNER_SECRET)
        recipient_cid = make_collection(client, sub=RECIPIENT, name="Meins")
        add_doc(client, recipient_cid, sub=RECIPIENT, text=RECIPIENT_TEXT)

        payload = run_knowledge_ask(client, sub=RECIPIENT, filters=filters)

    urls = [ref["url"] for ref in payload.get("references", [])]
    assert all(owner_doc not in url for url in urls), urls
    assert all(owner_cid not in url for url in urls), urls


def test_unscoped_chat_does_not_retrieve_foreign_documents():
    """The chat ask surface pins the same way: the owner's private chunk
    text never reaches the stranger's answer prompt."""
    client, llm, _store = make_ask_world()
    with client:
        owner_cid = make_collection(client, sub=OWNER, name="Privat")
        add_doc(client, owner_cid, sub=OWNER, text=OWNER_SECRET)
        recipient_cid = make_collection(client, sub=RECIPIENT, name="Meins")
        add_doc(client, recipient_cid, sub=RECIPIENT, text=RECIPIENT_TEXT)

        before = len(llm.prompts)
        answered = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "Wie lange ist die Frist?"}
                ],
                "mode": "knowledge",
            },
            headers=as_user(RECIPIENT),
        )
        assert answered.status_code == 200, answered.text

    # No prompt built while answering the stranger's ask may carry the
    # owner's private text — that would prove the chunk was retrieved.
    answer_prompts = llm.prompts[before:]
    assert answer_prompts, "the knowledge answer path must call the LLM"
    assert all("GEHEIMNIS_OWNER" not in prompt for prompt in answer_prompts)


def test_shared_in_collection_enters_unscoped_scope():
    """An accepted view grant makes the collection visible, so the
    stranger's UNSCOPED ask legitimately reaches it — the pin is scoped
    to visibility, not to ownership (no over-restriction)."""
    client, _llm, _store = make_ask_world()
    with client:
        owner_cid = make_collection(client, sub=OWNER, name="Geteilt")
        owner_doc = add_doc(client, owner_cid, sub=OWNER, text=OWNER_SECRET)
        accept_view_grant(client, owner_cid)

        payload = run_knowledge_ask(client, sub=RECIPIENT, filters=None)

    urls = [ref["url"] for ref in payload.get("references", [])]
    assert any(owner_doc in url for url in urls), urls


def test_explicit_invisible_collection_still_denied_on_both_asks():
    """Regression: the strict explicit gate is unchanged — a named
    invisible collection still denies the whole submission (404)."""
    client, _llm, _store = make_ask_world()
    with client:
        owner_cid = make_collection(client, sub=OWNER, name="Privat")
        add_doc(client, owner_cid, sub=OWNER, text=OWNER_SECRET)

        run_denied = client.post(
            "/v1/runs",
            json={
                "question": "Wie lange ist die Frist?",
                "mode": "knowledge",
                "knowledge_filters": {"collection_ids": [owner_cid]},
            },
            headers=as_user(RECIPIENT),
        )
        assert run_denied.status_code == 404

        chat_denied = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "Wie lange ist die Frist?"}
                ],
                "mode": "knowledge",
                "knowledge_filters": {"collection_ids": [owner_cid]},
            },
            headers=as_user(RECIPIENT),
        )
        assert chat_denied.status_code == 404


def test_mixed_model_visible_set_searches_every_visible_model_group():
    """Admission pins the full visible corpus and shared retrieval performs
    model-homogeneous searches before rank fusion.  No visible collection may
    disappear merely because it uses a non-default embedding model."""
    client, _llm, _store = make_ask_world()
    with client:
        recipient_cid = make_collection(client, sub=RECIPIENT, name="Meins")
        recipient_doc = add_doc(
            client, recipient_cid, sub=RECIPIENT, text=RECIPIENT_TEXT
        )
        legacy_cid = make_collection(
            client,
            sub=RECIPIENT,
            name="Altbestand",
            embedding_model="stub-embed-legacy",
        )
        legacy_text = "Historische Frist betraegt 72 Stunden."
        legacy_doc = add_doc(
            client,
            legacy_cid,
            sub=RECIPIENT,
            text=legacy_text,
        )

        implicit_payload = run_knowledge_ask(
            client, sub=RECIPIENT, filters=None
        )
        explicit_payload = run_knowledge_ask(
            client,
            sub=RECIPIENT,
            filters={"collection_ids": [recipient_cid, legacy_cid]},
        )

    for payload in (implicit_payload, explicit_payload):
        urls = [ref["url"] for ref in payload.get("references", [])]
        assert any(recipient_doc in url for url in urls), payload
        assert any(legacy_doc in url for url in urls), payload


def test_auth_off_unscoped_ask_stays_marker_silent(caplog):
    """The bypass marker must NOT fire for the sentinel principals of the
    none/apikey modes — there the unbounded scope IS the deliberate
    see-everything contract, and a warning on every normal ask would
    train operators to ignore the marker."""
    import logging as _logging

    with caplog.at_level(_logging.WARNING, logger="inqtrix"):
        payload = _auth_off_ask(None)
    assert payload.get("references"), payload
    assert "_knowledge_unscoped_principal" not in caplog.text


def _auth_off_ask(filters: dict | None) -> dict:
    client, _llm = make_knowledge_client()
    with client:
        _create_collection_with_document(client)
        body: dict = {"question": "Wie ist die Haftung geregelt?", "mode": "knowledge"}
        if filters is not None:
            body["knowledge_filters"] = filters
        created = client.post("/v1/runs", json=body)
        assert created.status_code == 202
        run_id = created.json()["run_id"]
        wait_for_run_status(client, run_id, "completed")
        return client.get(f"/v1/runs/{run_id}/result").json()


@pytest.mark.parametrize(
    "filters",
    [
        pytest.param(None, id="filters-absent"),
        pytest.param({"collection_ids": None}, id="collection-ids-null"),
    ],
)
def test_auth_off_unscoped_still_sees_everything(filters):
    """``visible_to=None`` (AUTH_MODE none) is the historical
    see-everything view: an omitted/null scope keeps searching every
    collection, so this deployment's single-user behaviour is unchanged."""
    payload = _auth_off_ask(filters)
    urls = [ref["url"] for ref in payload.get("references", [])]
    assert any("kd_" in url for url in urls), payload


def test_auth_off_explicit_empty_scope_stays_empty():
    """Pre-existing store contract, untouched by the pin: an EXPLICIT
    empty list means "nothing" (only ``None`` means everything), so the
    answer carries no references. Pinned here because the authenticated
    gate deliberately diverges — there ``[]`` expands to the visible set."""
    payload = _auth_off_ask({"collection_ids": []})
    assert payload.get("references", []) == [], payload
