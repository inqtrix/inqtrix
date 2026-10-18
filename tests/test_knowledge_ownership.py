"""Knowledge-collection ownership, direct sharing, and admission.

The visibility matrix uses canonical user UUIDs through the full OIDC
container. Owners manage their collections, strangers receive indistinct
404s, ownerless legacy rows remain available only to unscoped principals,
accepted view/edit shares grant their documented capabilities, collection
deletion revokes its shares, and ask paths deny invisible collections.
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
from inqtrix.auth.permissions import AuthorizationService, SharePermission
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentNotFound,
    KnowledgeProviderContext,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.runs.deletion_operations import DeletionOperationStore
from inqtrix.server.routers import asset_records, chat, knowledge, runs, sources
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import (
    KnowledgeStubLLM,
    StubEmbeddings,
    wait_for_deletion,
)
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    OidcHeaderProvider,
    user_headers,
)


class UnsafeCollectionSharingStore(MemoryKnowledgeStore):
    """Vector-only deployment double without an atomic metadata boundary."""

    @property
    def supports_collection_sharing(self) -> bool:
        return False


class PausedDeletionStore(DeletionOperationStore):
    """Retain queued work until the test explicitly crosses the worker boundary."""

    def _dispatch(self, record) -> None:
        del record


class FlakyDocumentDeletionStore(MemoryKnowledgeStore):
    """Fail one physical delete after the durable search fence has landed."""

    def __init__(self) -> None:
        super().__init__()
        self.delete_attempts = 0

    async def delete_document_for_aggregate(
        self, document_id: str, *, actor_user_id=None
    ) -> None:
        self.delete_attempts += 1
        if self.delete_attempts == 1:
            raise RuntimeError("temporary vector-store failure")
        await super().delete_document_for_aggregate(
            document_id,
            actor_user_id=actor_user_id,
        )


def make_world(
    store: MemoryKnowledgeStore | None = None,
    *,
    deletion_store: DeletionOperationStore | None = None,
) -> tuple[TestClient, MemoryKnowledgeStore]:
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
    store = store or MemoryKnowledgeStore()
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=OidcHeaderProvider(users),
        permissions=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        workspace_admin=identity,
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=store,
            default_top_k=4,
        ),
        deletion_store=deletion_store,
    )
    assert container.share_service is not None
    if store.supports_collection_sharing:
        assert "knowledge_collection" in container.share_service.resource_types
    else:
        assert "knowledge_collection" not in container.share_service.resource_types
    app = FastAPI()
    app.include_router(asset_records.build_router(container))
    app.include_router(knowledge.build_router(container))
    app.include_router(sources.build_router(container))
    app.include_router(runs.build_router(container))
    app.include_router(chat.build_router(container))
    app.include_router(build_shares_router(container))
    return TestClient(app), store


@pytest.fixture()
def world():
    return make_world()


def as_user(user_id: uuid.UUID) -> dict[str, str]:
    """Return the canonical identity header for one scoped request."""
    return user_headers(user_id)


def create_collection(
    client: TestClient,
    *,
    sub: uuid.UUID,
    name: str = "Meine Sammlung",
) -> str:
    response = client.post(
        "/v1/knowledge/collections", json={"name": name}, headers=as_user(sub)
    )
    assert response.status_code == 201
    return response.json()["id"]


def add_document(
    client: TestClient,
    collection_id: str,
    *,
    sub: uuid.UUID,
):
    return client.post(
        f"/v1/knowledge/collections/{collection_id}/documents",
        json={"title": "Notiz", "text": "Die Frist betraegt 24 Stunden."},
        headers=as_user(sub),
    )


def grant(client: TestClient, collection_id: str, *, permission: str = "view"):
    response = client.post(
        "/v1/shares",
        json={
            "resource_type": "knowledge_collection",
            "resource_id": collection_id,
            "invitees": [
                {"user_id": str(RECIPIENT), "permission": permission}
            ],
        },
        headers=as_user(OWNER),
    )
    if response.status_code != 201:
        return response
    share_id = response.json()["data"][0]["id"]
    accepted = client.post(
        f"/v1/shares/{share_id}/accept", headers=as_user(RECIPIENT)
    )
    assert accepted.status_code == 200
    return response


def test_vector_only_collection_sharing_returns_501() -> None:
    client, _store = make_world(UnsafeCollectionSharingStore())
    collection_id = create_collection(client, sub=OWNER)

    response = grant(client, collection_id)

    assert response.status_code == 501
    assert response.json()["error"]["type"] == "unsupported"


def test_owner_sees_collection_stranger_does_not(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    add_document(client, collection_id, sub=OWNER)

    owner_listed = client.get(
        "/v1/knowledge/collections", headers=as_user(OWNER)
    ).json()["data"]
    assert [item["id"] for item in owner_listed] == [collection_id]
    assert owner_listed[0]["access"] == {"mode": "owner"}

    stranger_listed = client.get(
        "/v1/knowledge/collections", headers=as_user(RECIPIENT)
    ).json()["data"]
    assert stranger_listed == []

    denied_docs = client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(RECIPIENT),
    )
    assert denied_docs.status_code == 404
    denied_delete = client.delete(
        f"/v1/knowledge/collections/{collection_id}",
        headers=as_user(RECIPIENT),
    )
    assert denied_delete.status_code == 404
    # The collection survives the stranger's delete attempt.
    assert client.get(
        "/v1/knowledge/collections", headers=as_user(OWNER)
    ).json()["data"]


def test_foreign_asset_delete_is_non_disclosing_and_non_mutating(world):
    client, _store = world
    owner_headers = {
        **as_user(OWNER),
        "X-Inqtrix-Workspace-Id": "ws-owner",
    }
    stranger_headers = {
        **as_user(RECIPIENT),
        "X-Inqtrix-Workspace-Id": "ws-stranger",
    }
    assert client.put(
        "/v1/assets/sections/sec_private",
        headers=owner_headers,
        json={
            "kind": "custom",
            "title": "Private files",
            "created_at": 1.0,
            "updated_at": 1.0,
        },
    ).status_code == 200
    assert client.put(
        "/v1/assets/asset_private",
        headers=owner_headers,
        json={
            "section_id": "sec_private",
            "group_id": None,
            "title": "Private",
            "label": "private",
            "file_name": "private.txt",
            "mime_type": "text/plain",
            "origin": "library",
            "page_count": None,
            "parse_status": "parsed",
            "parse_warning": None,
            "text_truncated": False,
            "size_bytes": 7,
            "server_file_id": None,
            "parser_id": "client",
            "extracted_text": "private",
            "created_at": 1.0,
            "updated_at": 1.0,
        },
    ).status_code == 200

    denied = client.delete(
        "/v1/assets/asset_private",
        headers=stranger_headers,
    )

    assert denied.status_code == 404
    assert denied.json()["error"]["type"] == "not_found"
    assert client.get(
        "/v1/assets/asset_private",
        headers=owner_headers,
    ).status_code == 200
    assert client.get(
        "/v1/deletion-operations",
        headers=stranger_headers,
    ).json()["data"] == []


def test_document_reads_deny_via_parent(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]

    denied_text = client.get(
        f"/v1/knowledge/documents/{document_id}/text",
        headers=as_user(RECIPIENT),
    )
    assert denied_text.status_code == 404
    missing = client.get(
        "/v1/knowledge/documents/kd_does_not_exist/text",
        headers=as_user(RECIPIENT),
    )
    # Denial and absence are byte-identical.
    assert denied_text.json() == missing.json()

    # The chunk view scopes exactly like the text route.
    denied_chunk = client.get(
        f"/v1/knowledge/documents/{document_id}/chunks/0",
        headers=as_user(RECIPIENT),
    )
    assert denied_chunk.status_code == 404
    assert denied_chunk.json() == missing.json()


def test_source_view_denies_stranger_via_parent(world):
    """``/v1/sources/{id}`` is scoped exactly like the document
    endpoints: the owner reads the citable view, a stranger gets the
    same 404 an unknown id produces."""
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]

    owner_view = client.get(
        f"/v1/sources/{document_id}", headers=as_user(OWNER)
    )
    assert owner_view.status_code == 200
    assert owner_view.json()["collection_id"] == collection_id

    denied = client.get(
        f"/v1/sources/{document_id}", headers=as_user(RECIPIENT)
    )
    assert denied.status_code == 404
    missing = client.get(
        "/v1/sources/kd_does_not_exist", headers=as_user(RECIPIENT)
    )
    # Denial and absence are byte-identical.
    assert denied.json() == missing.json()


def test_source_view_admits_accepted_share_not_pending(world):
    """A pending grant admits nothing; acceptance opens the source
    view via the parent collection's view grant."""
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]

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

    pending_denied = client.get(
        f"/v1/sources/{document_id}", headers=as_user(RECIPIENT)
    )
    assert pending_denied.status_code == 404

    share_id = granted.json()["data"][0]["id"]
    accepted = client.post(
        f"/v1/shares/{share_id}/accept", headers=as_user(RECIPIENT)
    )
    assert accepted.status_code == 200

    admitted = client.get(
        f"/v1/sources/{document_id}", headers=as_user(RECIPIENT)
    )
    assert admitted.status_code == 200
    assert "Frist" in admitted.json()["text"]


def test_ownerless_collections_are_unscoped_only(world):
    client, store = world
    legacy = asyncio.run(
        store.create_collection(
            name="Bestand", embedding_model="stub-embed-8", embedding_dim=8
        )
    )
    assert legacy.created_by_user_id is None

    for user_id in (OWNER, RECIPIENT):
        listed = client.get(
            "/v1/knowledge/collections", headers=as_user(user_id)
        ).json()["data"]
        assert listed == []
        assert add_document(client, legacy.id, sub=user_id).status_code == 404

    unscoped = client.get("/v1/knowledge/collections").json()["data"]
    assert [item["id"] for item in unscoped] == [legacy.id]
    assert unscoped[0]["access"] == {"mode": "unscoped"}
    assert client.post(
        f"/v1/knowledge/collections/{legacy.id}/documents",
        json={"title": "Notiz", "text": "Unscoped legacy write."},
    ).status_code == 201


def test_view_grant_admits_reads_not_writes(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]
    assert grant(client, collection_id).status_code == 201

    listed = client.get(
        "/v1/knowledge/collections", headers=as_user(RECIPIENT)
    ).json()["data"]
    assert [item["id"] for item in listed] == [collection_id]
    assert listed[0]["access"] == {"mode": "shared", "permission": "view"}

    docs = client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(RECIPIENT),
    )
    assert docs.status_code == 200
    text = client.get(
        f"/v1/knowledge/documents/{document_id}/text",
        headers=as_user(RECIPIENT),
    )
    assert text.status_code == 200

    assert add_document(client, collection_id, sub=RECIPIENT).status_code == 404
    assert (
        client.delete(
            f"/v1/knowledge/documents/{document_id}",
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/v1/knowledge/collections/{collection_id}",
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )


def test_edit_grant_admits_document_writes_not_deletion(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    assert grant(client, collection_id, permission="edit").status_code == 201

    created = add_document(client, collection_id, sub=RECIPIENT)
    assert created.status_code == 201
    deleted = client.delete(
        f"/v1/knowledge/documents/{created.json()['id']}",
        headers=as_user(RECIPIENT),
    )
    wait_for_deletion(client, deleted, headers=as_user(RECIPIENT))
    # Deleting the COLLECTION stays owner-only even with edit.
    assert (
        client.delete(
            f"/v1/knowledge/collections/{collection_id}",
            headers=as_user(RECIPIENT),
        ).status_code
        == 404
    )


def test_document_delete_revalidates_acl_before_destructive_progress() -> None:
    deletions = PausedDeletionStore()
    client, _store = make_world(deletion_store=deletions)
    collection_id = create_collection(client, sub=OWNER)
    granted = grant(client, collection_id, permission="edit")
    share_id = granted.json()["data"][0]["id"]
    created = add_document(client, collection_id, sub=RECIPIENT)
    document_id = created.json()["id"]

    started = client.delete(
        f"/v1/knowledge/documents/{document_id}",
        headers=as_user(RECIPIENT),
    )
    assert started.status_code == 202
    assert client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(OWNER),
    ).json()["data"] == []

    revoked = client.delete(
        f"/v1/shares/{share_id}",
        headers=as_user(OWNER),
    )
    assert revoked.status_code == 204
    operation_id = started.json()["operation_id"]
    deletions._run(operation_id)

    receipt = client.get(
        f"/v1/deletion-operations/{operation_id}",
        headers=as_user(RECIPIENT),
    ).json()
    assert receipt["status"] == "delete_failed"
    assert receipt["completed_items"] == 0
    restored = client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(OWNER),
    ).json()["data"]
    assert [item["id"] for item in restored] == [document_id]

    assert grant(client, collection_id, permission="edit").status_code == 201
    retried = client.post(
        f"/v1/deletion-operations/{operation_id}/retry",
        headers=as_user(RECIPIENT),
    )
    assert retried.status_code == 202
    # Retry re-establishes the search tombstone before a worker may run.
    assert client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(OWNER),
    ).json()["data"] == []
    deletions._run(operation_id)
    receipt = client.get(
        f"/v1/deletion-operations/{operation_id}",
        headers=as_user(RECIPIENT),
    ).json()
    assert receipt["status"] == "deleted"


def test_failed_document_delete_stays_hidden_and_retries_same_receipt() -> None:
    store = FlakyDocumentDeletionStore()
    client, _ = make_world(store)
    collection_id = create_collection(client, sub=OWNER)
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]
    headers = {
        **as_user(OWNER),
        "X-Inqtrix-Workspace-Id": "ws-current-ui-context",
    }

    started = client.delete(
        f"/v1/knowledge/documents/{document_id}",
        headers=headers,
    )
    assert started.status_code == 202
    operation_id = started.json()["operation_id"]
    deadline = time.monotonic() + 3
    receipt = None
    while time.monotonic() < deadline:
        receipt = client.get(
            f"/v1/deletion-operations/{operation_id}",
            headers=headers,
        ).json()
        if receipt["status"] == "delete_failed":
            break
        time.sleep(0.01)
    assert receipt is not None and receipt["status"] == "delete_failed"
    assert receipt["completed_items"] == 2
    assert client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(OWNER),
    ).json()["data"] == []
    feed = client.get("/v1/deletion-operations", headers=headers)
    assert feed.status_code == 200
    assert operation_id in {
        operation["operation_id"] for operation in feed.json()["data"]
    }

    retried = client.post(
        f"/v1/deletion-operations/{operation_id}/retry",
        headers=headers,
    )
    completed = wait_for_deletion(client, retried, headers=headers)
    assert completed["operation_id"] == operation_id
    assert store.delete_attempts == 2


def test_partial_document_delete_converges_after_editor_share_is_revoked() -> None:
    store = FlakyDocumentDeletionStore()
    client, _ = make_world(store)
    collection_id = create_collection(client, sub=OWNER)
    granted = grant(client, collection_id, permission="edit")
    share_id = granted.json()["data"][0]["id"]
    document_id = add_document(client, collection_id, sub=OWNER).json()["id"]

    started = client.delete(
        f"/v1/knowledge/documents/{document_id}",
        headers=as_user(RECIPIENT),
    )
    assert started.status_code == 202
    operation_id = started.json()["operation_id"]
    deadline = time.monotonic() + 3
    receipt = None
    while time.monotonic() < deadline:
        receipt = client.get(
            f"/v1/deletion-operations/{operation_id}",
            headers=as_user(RECIPIENT),
        ).json()
        if receipt["status"] == "delete_failed":
            break
        time.sleep(0.01)
    assert receipt is not None and receipt["status"] == "delete_failed"
    assert receipt["completed_items"] == 2

    assert client.delete(
        f"/v1/shares/{share_id}", headers=as_user(OWNER)
    ).status_code == 204
    retried = client.post(
        f"/v1/deletion-operations/{operation_id}/retry",
        headers=as_user(RECIPIENT),
    )
    completed = wait_for_deletion(
        client,
        retried,
        headers=as_user(RECIPIENT),
    )
    assert completed["operation_id"] == operation_id
    assert client.get(
        f"/v1/knowledge/collections/{collection_id}/documents",
        headers=as_user(OWNER),
    ).json()["data"] == []


def test_memory_store_revocation_fences_all_collection_writes() -> None:
    """A stale service check cannot write after the direct share is revoked."""
    identity = MemoryIdentityStore()
    store = MemoryKnowledgeStore()
    store.bind_authorization(
        resource_access_guard=identity.resource_access_guard_sync
    )
    collection = asyncio.run(
        store.create_collection(
            name="Shared",
            embedding_model="stub-embed-8",
            embedding_dim=8,
            created_by_user_id=OWNER,
        )
    )
    document = asyncio.run(
        store.add_document(
            collection_id=collection.id,
            title="Original",
            text="Original text",
            metadata={},
            chunks=["Original text"],
            embeddings=[[0.0] * 8],
            actor_user_id=OWNER,
        )
    )
    identity.add_share(
        recipient_user_id=RECIPIENT,
        resource_type="knowledge_collection",
        resource_id=collection.id,
        permission=SharePermission.EDIT,
        granted_by_user_id=OWNER,
    )
    assert identity.permission_for_sync(
        tenant_id="default",
        resource_type="knowledge_collection",
        resource_id=collection.id,
        recipient_user_id=RECIPIENT,
    ) is SharePermission.EDIT

    identity.revoke_share(
        recipient_user_id=RECIPIENT,
        resource_type="knowledge_collection",
        resource_id=collection.id,
    )

    with pytest.raises(CollectionNotFound):
        asyncio.run(
            store.add_document(
                collection_id=collection.id,
                title="Too late",
                text="Stale write",
                metadata={},
                chunks=["Stale write"],
                embeddings=[[0.0] * 8],
                actor_user_id=RECIPIENT,
            )
        )
    with pytest.raises(DocumentNotFound):
        asyncio.run(
            store.reembed_document(
                document_id=document.id,
                chunks=["Changed"],
                embeddings=[[1.0] * 8],
                actor_user_id=RECIPIENT,
            )
        )
    with pytest.raises(DocumentNotFound):
        asyncio.run(
            store.delete_document(
                document.id,
                actor_user_id=RECIPIENT,
            )
        )

    remaining = asyncio.run(store.list_documents(collection.id))
    assert [item.id for item in remaining] == [document.id]
    assert remaining[0].text == "Original text"


def test_search_filters_to_visible_collections(world):
    client, _store = world
    owned = create_collection(client, sub=OWNER)
    add_document(client, owned, sub=OWNER)
    recipient_owned = create_collection(
        client, sub=RECIPIENT, name="Eigene Sammlung"
    )
    add_document(client, recipient_owned, sub=RECIPIENT)

    # Explicitly asking for owned + inaccessible: the invisible owner id is
    # filtered and the recipient's own results come back.
    mixed = client.post(
        "/v1/knowledge/search",
        json={
            "query": "Frist",
            "collection_ids": [owned, recipient_owned],
        },
        headers=as_user(RECIPIENT),
    )
    assert mixed.status_code == 200
    mixed_body = mixed.json()
    assert all(
        hit["collection_id"] == recipient_owned
        for hit in mixed_body["data"]
    )
    # The filter is not silent: the dropped id surfaces as a warning so
    # an agent planning against the results sees it.
    filtered = [
        warning
        for warning in mixed_body["warnings"]
        if warning["code"] == "collections_filtered"
    ]
    assert filtered and filtered[0]["filtered_ids"] == [owned]

    # Asking ONLY for the invisible collection is the indistinct 404.
    denied = client.post(
        "/v1/knowledge/search",
        json={"query": "Frist", "collection_ids": [owned]},
        headers=as_user(RECIPIENT),
    )
    assert denied.status_code == 404

    # A scoped search without ids ranges over the current visible set only.
    visible = client.post(
        "/v1/knowledge/search",
        json={"query": "Frist"},
        headers=as_user(RECIPIENT),
    )
    assert visible.status_code == 200
    assert all(
        hit["collection_id"] == recipient_owned
        for hit in visible.json()["data"]
    )


def test_collection_deletion_revokes_its_shares(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)
    assert grant(client, collection_id).status_code == 201
    before = client.get(
        "/v1/shares/inbox",
        headers=as_user(RECIPIENT),
    ).json()["data"]
    assert [item["resource_id"] for item in before["accepted"]] == [
        collection_id
    ]

    deleted = client.delete(
        f"/v1/knowledge/collections/{collection_id}", headers=as_user(OWNER)
    )
    wait_for_deletion(client, deleted, headers=as_user(OWNER))
    after = client.get(
        "/v1/shares/inbox",
        headers=as_user(RECIPIENT),
    ).json()["data"]
    assert after == {"pending": [], "accepted": []}


def test_run_submission_denies_invisible_collections(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)

    denied = client.post(
        "/v1/runs",
        json={
            "question": "Wie lange ist die Frist?",
            "mode": "knowledge",
            "knowledge_filters": {"collection_ids": [collection_id]},
        },
        headers=as_user(RECIPIENT),
    )
    assert denied.status_code == 404

    assert grant(client, collection_id).status_code == 201
    admitted = client.post(
        "/v1/runs",
        json={
            "question": "Wie lange ist die Frist?",
            "mode": "knowledge",
            "knowledge_filters": {"collection_ids": [collection_id]},
        },
        headers=as_user(RECIPIENT),
    )
    assert admitted.status_code == 202


def test_chat_submission_denies_invisible_collections(world):
    client, _store = world
    collection_id = create_collection(client, sub=OWNER)

    denied = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {"role": "user", "content": "Wie lange ist die Frist?"}
            ],
            "mode": "knowledge",
            "knowledge_filters": {"collection_ids": [collection_id]},
        },
        headers=as_user(RECIPIENT),
    )
    assert denied.status_code == 404


def test_legacy_collections_are_not_shareable(world):
    client, store = world
    legacy = asyncio.run(
        store.create_collection(
            name="Bestand", embedding_model="stub-embed-8", embedding_dim=8
        )
    )
    # No owner means no scoped grant authority.
    assert grant(client, legacy.id).status_code == 404
