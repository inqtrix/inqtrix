"""HTTP integration for the background reindex endpoints.

Exercises the real router + container wiring (the in-memory knowledge
store, the indexing service, route registration) end-to-end over a
FastAPI ``TestClient``: create a collection, ingest documents, start a
reindex, poll it to completion, list it, and stream its events.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.knowledge.contextualize import ContextualizationDependencyError
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.knowledge.parsing import DocumentParser
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers import indexing, knowledge
from inqtrix.server.routers import asset_records as asset_records_router
from inqtrix.server.routers import files as files_router
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.test_knowledge_routes import KnowledgeStubLLM, StubEmbeddings, _StubSearch
from tests.test_runs_sharing import (
    OWNER,
    RECIPIENT,
    OidcHeaderProvider,
    user_headers,
)


class _PreparedSourceParser(DocumentParser):
    @property
    def parser_id(self) -> str:
        return "prepared-source-test"

    @property
    def parser_revision(self) -> str:
        return "2.0"

    def parse(self, *, file_name: str, content: bytes) -> str:
        del file_name
        return content.decode("utf-8")


def make_indexing_client(
    *,
    embeddings: StubEmbeddings | None = None,
    document_parser: DocumentParser | None = None,
) -> TestClient:
    """Build a TestClient exposing the knowledge + reindex routers."""
    identity = MemoryIdentityStore()
    users = MemoryUserDirectory()

    async def mirror_users() -> None:
        for user_id, subject, display_name in (
            (OWNER, "user-owner", "Olga Owner"),
            (RECIPIENT, "user-recipient", "Rita Recipient"),
        ):
            await users.record_login(
                tenant_id="default",
                issuer="http://idp.example",
                subject=subject,
                email=f"{subject}@example.com",
                email_verified=True,
                display_name=display_name,
                canonical_user_id=user_id,
            )

    asyncio.run(mirror_users())
    auth_provider = OidcHeaderProvider(users)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=_StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=auth_provider,
        permissions=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        workspace_admin=identity,
        knowledge=KnowledgeProviderContext(
            embeddings=embeddings or StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=4,
        ),
        document_parser=document_parser,
    )
    assert container.indexing_service is not None
    app = FastAPI()
    app.state.container = container
    app.include_router(files_router.build_router(container))
    app.include_router(asset_records_router.build_router(container))
    app.include_router(knowledge.build_router(container))
    app.include_router(indexing.build_router(container))
    app.include_router(build_shares_router(container))
    client = TestClient(app, headers=user_headers(OWNER))
    client.auth_provider = auth_provider  # type: ignore[attr-defined]
    return client


def _collection_with_two_documents(client: TestClient) -> str:
    created = client.post("/v1/knowledge/collections", json={"name": "Vertraege"})
    assert created.status_code == 201
    collection_id = created.json()["id"]
    for index in range(2):
        ingested = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"title": f"Doc {index}", "text": f"alpha beta gamma {index}"},
        )
        assert ingested.status_code == 201
    return collection_id


def _poll_until_terminal(
    client: TestClient, job_id: str, *, timeout: float = 2.0
) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/knowledge/indexing-jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {
            "completed",
            "failed",
            "cancelled",
            "superseded",
            "ready_raw_by_user_choice",
        }:
            return body
        time.sleep(0.02)
    raise AssertionError("reindex job did not reach a terminal status in time")


def _poll_deletion_until_terminal(
    client: TestClient, operation_id: str, *, timeout: float = 2.0
) -> dict:
    deadline = time.time() + timeout
    latest = {}
    while time.time() < deadline:
        response = client.get(f"/v1/deletion-operations/{operation_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] in {"deleted", "delete_failed"}:
            return latest
        time.sleep(0.01)
    raise AssertionError(
        f"deletion {operation_id} did not reach a terminal status: {latest}"
    )


def test_reindex_completes_over_http() -> None:
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)

        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex",
            json={"index_id": "vix_1"},
        )
        assert started.status_code == 202
        summary = started.json()
        assert summary["collection_id"] == collection_id
        assert summary["index_id"] == "vix_1"
        job_id = summary["job_id"]

        final = _poll_until_terminal(client, job_id)
        assert final["status"] == "completed"
        assert final["total_documents"] == 2
        assert final["completed_documents"] == 2
        assert final["percent"] == 100


def test_resume_unavailable_is_typed_and_keeps_the_job_paused() -> None:
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)
        container = client.app.state.container
        job_store = container.indexing_service.job_store

        def invalid_revision(handle):
            handle.begin(1)
            handle.pause_validation("reserved identity missing")

        summary = job_store.submit(
            collection_id=collection_id,
            collection_name="Vertraege",
            embedding_model="stub-embed-8",
            operation_kind="document_revision",
            document_id=None,
            revision_id=None,
            work=invalid_revision,
        )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = client.get(
                f"/v1/knowledge/indexing-jobs/{summary['job_id']}"
            ).json()
            if current["status"] == "paused_validation":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("invalid revision did not pause")

        response = client.post(
            f"/v1/knowledge/indexing-jobs/{summary['job_id']}/resume"
        )
        assert response.status_code == 409
        assert response.json()["error"]["type"] == "resume_unavailable"
        after = client.get(f"/v1/knowledge/indexing-jobs/{summary['job_id']}").json()
        assert after["status"] == "paused_validation"


def test_document_revision_route_reserves_identity_before_background_work() -> None:
    client = make_indexing_client()
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/document-revisions",
            json={
                "title": "Rahmenvertrag",
                "text": "Die Haftung ist begrenzt.",
                "metadata": {
                    "source_id": "document:rahmenvertrag",
                    # Only the server states extraction facts.
                    "extraction": {"parser_id": "forged", "pages": 1},
                },
            },
        )

        assert started.status_code == 202
        summary = started.json()
        assert summary["operation_kind"] == "document_revision"
        assert summary["document_id"].startswith("kd_")
        assert summary["revision_id"].startswith("rev_")
        assert summary["generation_id"] is None

        final = _poll_until_terminal(client, summary["job_id"])
        assert final["status"] == "completed"
        documents = client.get(
            f"/v1/knowledge/collections/{collection_id}/documents"
        ).json()["data"]
        assert [document["id"] for document in documents] == [summary["document_id"]]
        assert documents[0]["metadata"]["source_id"] == "document:rahmenvertrag"
        assert "extraction" not in documents[0]["metadata"]


def test_async_document_route_requires_prepared_text_for_registered_files() -> None:
    client = make_indexing_client()
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/document-revisions",
            json={"title": "Datei", "file_id": "file_1"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "file_preparation_required"


def test_async_document_route_reserves_only_operation_fenced_asset_text() -> None:
    client = make_indexing_client(document_parser=_PreparedSourceParser())
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        section = client.put(
            "/v1/assets/sections/fsec_sources",
            json={
                "kind": "custom",
                "title": "Sources",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        assert section.status_code == 200
        uploaded = client.post(
            "/v1/files",
            files={
                "file": (
                    "source.txt",
                    b"Canonical server prepared source text.",
                    "text/plain",
                )
            },
            data={
                "asset_id": "asset-source",
                "section_id": "fsec_sources",
                "title": "Source",
                "label": "source",
                "origin": "library",
                "created_at": "1.0",
                "updated_at": "1.0",
            },
        )
        assert uploaded.status_code == 202
        operation_id = uploaded.json()["upload_operation"]["operation_id"]

        # The revision cannot reserve browser/display text while canonical
        # preparation is still queued.
        pending = client.post(
            f"/v1/knowledge/collections/{collection_id}/document-revisions",
            json={"asset_id": "asset-source", "title": "Source"},
        )
        assert pending.status_code == 409
        assert pending.json()["error"]["type"] == "source_preparation_pending"

        upload_service = client.app.state.container.upload_operation_service
        claimed = upload_service.operations.claim_for_execution(
            operation_id,
            "default",
            allow_takeover=False,
        )
        assert claimed is not None
        upload_service.execute_claimed(claimed)

        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/document-revisions",
            json={
                "asset_id": "asset-source",
                "title": "Source",
                "metadata": {
                    "untrusted": "display-only",
                    "extraction": {"parser_id": "forged"},
                },
            },
        )
        assert started.status_code == 202
        summary = started.json()
        final = _poll_until_terminal(client, summary["job_id"])
        assert final["status"] == "completed"
        document = client.get(
            f"/v1/knowledge/documents/{summary['document_id']}/text"
        ).json()

    assert document["text"] == "Canonical server prepared source text."
    assert document["metadata"]["source_id"] == "asset:asset-source"
    assert document["metadata"]["source_parser_id"] == "prepared-source-test"
    assert document["metadata"]["extraction"] == {
        "parser_id": "prepared-source-test",
        "parser_revision": "2.0",
        "file_sha256": hashlib.sha256(
            b"Canonical server prepared source text."
        ).hexdigest(),
    }


def test_async_document_route_repairs_legacy_asset_from_server_original() -> None:
    """Pre-operation assets are repaired from bytes, never browser text."""

    canonical_text = "Canonical text from the immutable server original."
    client = make_indexing_client(document_parser=_PreparedSourceParser())
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        section = client.put(
            "/v1/assets/sections/fsec_legacy_sources",
            json={
                "kind": "custom",
                "title": "Legacy Sources",
                "created_at": 1.0,
                "updated_at": 1.0,
            },
        )
        assert section.status_code == 200
        original = client.post(
            "/v1/files",
            files={
                "file": (
                    "legacy-source.txt",
                    canonical_text.encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert original.status_code == 201
        file_record = original.json()
        # Seed the exact persisted shape produced by releases before durable
        # upload operations. The public asset PUT deliberately refuses clients
        # that try to bind arbitrary server_file_id values.
        legacy = asyncio.run(
            client.app.state.container.asset_records_service.store.upsert_asset(
                id="asset-legacy-source",
                section_id="fsec_legacy_sources",
                group_id=None,
                title="Legacy Source",
                label="legacy-source",
                file_name="legacy-source.txt",
                mime_type="text/plain",
                origin="library",
                page_count=None,
                parse_status="parsed",
                parse_warning=None,
                text_truncated=False,
                size_bytes=len(canonical_text),
                server_file_id=file_record["id"],
                parser_id="browser-display-parser",
                extracted_text="UNTRUSTED BROWSER TEXT MUST NOT BE INDEXED",
                created_at=1.0,
                updated_at=1.0,
                created_by_user_id=OWNER,
                workspace_id=None,
            )
        )
        assert legacy.upload_operation_id is None
        assert legacy.prepared_text == ""

        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/document-revisions",
            json={"asset_id": "asset-legacy-source", "title": "Legacy Source"},
        )
        assert started.status_code == 202, started.text
        summary = started.json()
        final = _poll_until_terminal(client, summary["job_id"])
        assert final["status"] == "completed"

        asset = client.get("/v1/assets/asset-legacy-source").json()
        document = client.get(
            f"/v1/knowledge/documents/{summary['document_id']}/text"
        ).json()

    assert asset["prepared_text"] == canonical_text
    assert asset["prepared_parser_id"] == "prepared-source-test"
    # The repair is a new extraction, so it records the revision that made it.
    assert asset["prepared_parser_revision"] == "2.0"
    assert document["metadata"]["extraction"]["parser_revision"] == "2.0"
    assert asset["prepared_content_hash"] == hashlib.sha256(
        canonical_text.encode("utf-8")
    ).hexdigest()
    assert document["text"] == canonical_text
    assert "UNTRUSTED BROWSER TEXT" not in document["text"]
    assert document["metadata"]["source_id"] == "asset:asset-legacy-source"
    assert document["metadata"]["source_parser_id"] == "prepared-source-test"


def test_legacy_document_adapter_exposes_paused_job_identity(monkeypatch) -> None:
    client = make_indexing_client()
    indexing_service = client.app.state.container.indexing_service

    async def paused_revision(**_kwargs):
        return {
            "collection_id": "kc_paused",
            "document_id": "kd_paused",
            "error": {
                "message": "provider socket timed out",
                "type": "dependency_timeout",
            },
            "events_url": "/v1/knowledge/indexing-jobs/ix_paused/events",
            "job_id": "ix_paused",
            "operation_kind": "document_revision",
            "revision_id": "rev_paused",
            "status": "paused_dependency",
        }

    monkeypatch.setattr(
        indexing_service,
        "submit_document_revision",
        paused_revision,
    )
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"title": "Source", "text": "source text"},
        )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "message": "provider socket timed out",
        "type": "contextualization_dependency_error",
        "job_id": "ix_paused",
        "job_status": "paused_dependency",
        "document_id": "kd_paused",
        "revision_id": "rev_paused",
        "events_url": "/v1/knowledge/indexing-jobs/ix_paused/events",
    }


@pytest.mark.parametrize(
    "error_type",
    [
        "contextualization_provider_timeout",
        "contextualization_provider_rate_limited",
        "contextualization_provider_unavailable",
        "contextualization_provider_circuit_open",
        "contextualization_circuit_state_unavailable",
    ],
)
def test_document_ingestion_forwards_precise_contextualization_dependency_type(
    monkeypatch,
    error_type: str,
) -> None:
    client = make_indexing_client()
    indexing_service = client.app.state.container.indexing_service

    async def fail_revision(**_kwargs):
        raise ContextualizationDependencyError(error_type=error_type)

    monkeypatch.setattr(
        indexing_service,
        "submit_document_revision",
        fail_revision,
    )
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"title": "Source", "text": "source text"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == error_type


def test_list_jobs_filters_by_collection() -> None:
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)
        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex", json={}
        )
        job_id = started.json()["job_id"]
        _poll_until_terminal(client, job_id)

        listed = client.get("/v1/knowledge/indexing-jobs")
        assert listed.status_code == 200
        ids = {job["job_id"] for job in listed.json()["data"]}
        assert job_id in ids

        filtered = client.get(
            "/v1/knowledge/indexing-jobs",
            params={"collection_id": collection_id},
        )
        assert {job["job_id"] for job in filtered.json()["data"]} == {job_id}

        other = client.get(
            "/v1/knowledge/indexing-jobs",
            params={"collection_id": "kc_other"},
        )
        assert other.json()["data"] == []


def test_reindex_unknown_collection_returns_404() -> None:
    client = make_indexing_client()
    with client:
        response = client.post("/v1/knowledge/collections/kc_unknown/reindex", json={})
    assert response.status_code == 404
    assert response.json() == {
        "error": {"message": "Collection not found", "type": "not_found"}
    }


def test_reindex_events_stream_replays_completion() -> None:
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)
        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex", json={}
        )
        job_id = started.json()["job_id"]
        _poll_until_terminal(client, job_id)

        events = client.get(f"/v1/knowledge/indexing-jobs/{job_id}/events")
        assert events.status_code == 200
        body = events.text
        assert "inqtrix.index.started" in body
        assert "inqtrix.index.completed" in body


def test_reindex_events_resume_after_last_event_id_without_visual_replay() -> None:
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)
        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex", json={}
        )
        job_id = started.json()["job_id"]
        summary = _poll_until_terminal(client, job_id)
        last_sequence = summary["last_event_sequence"]

        tail = client.get(
            f"/v1/knowledge/indexing-jobs/{job_id}/events",
            headers={"Last-Event-ID": str(last_sequence - 1)},
        )
        current = client.get(
            f"/v1/knowledge/indexing-jobs/{job_id}/events",
            headers={"Last-Event-ID": str(last_sequence)},
        )
        invalid = client.get(
            f"/v1/knowledge/indexing-jobs/{job_id}/events",
            headers={"Last-Event-ID": "not-a-sequence"},
        )

    assert tail.status_code == 200
    assert "inqtrix.index.completed" in tail.text
    assert "inqtrix.index.started" not in tail.text
    assert current.status_code == 200
    assert current.text == ""
    assert invalid.status_code == 400


def test_document_revision_event_stream_terminates_on_superseded() -> None:
    client = make_indexing_client()
    with client:
        collection_id = client.post(
            "/v1/knowledge/collections", json={"name": "Vertraege"}
        ).json()["id"]
        jobs = client.app.state.container.indexing_service.job_store

        def supersede(handle) -> None:
            handle.begin(1)
            handle.supersede()

        summary = jobs.submit(
            collection_id=collection_id,
            collection_name="Vertraege",
            embedding_model="stub-embed-8",
            operation_kind="document_revision",
            document_id="kd_superseded",
            revision_id="rev_superseded",
            created_by_user_id=OWNER,
            created_by_tenant_id="default",
            work=supersede,
        )
        _poll_until_terminal(client, summary["job_id"])
        events = client.get(f"/v1/knowledge/indexing-jobs/{summary['job_id']}/events")

    assert events.status_code == 200
    assert "inqtrix.index.superseded" in events.text


def test_reindex_stream_stops_before_replay_when_credential_is_revoked() -> None:
    """A job stream emits no replay frame after live credential revocation."""
    client = make_indexing_client()
    with client:
        collection_id = _collection_with_two_documents(client)
        started = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex", json={}
        )
        job_id = started.json()["job_id"]
        _poll_until_terminal(client, job_id)
        client.auth_provider.revoke_live = True  # type: ignore[attr-defined]

        events = client.get(f"/v1/knowledge/indexing-jobs/{job_id}/events")

    assert events.status_code == 200
    assert events.text == ""


def test_get_unknown_job_returns_404() -> None:
    client = make_indexing_client()
    with client:
        response = client.get("/v1/knowledge/indexing-jobs/ix_missing")
    assert response.status_code == 404


def test_active_reindex_accepts_document_deletion_delta(
    monkeypatch,
) -> None:
    client = make_indexing_client()
    container = client.app.state.container
    service = container.knowledge_service
    assert service is not None
    started = threading.Event()
    release = threading.Event()
    real_reembed = service.reembed_document_with_receipt

    async def blocking_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(
        service,
        "reembed_document_with_receipt",
        blocking_reembed,
    )
    with client:
        collection_id = _collection_with_two_documents(client)
        documents = client.get(
            f"/v1/knowledge/collections/{collection_id}/documents"
        ).json()["data"]
        document_id = documents[0]["id"]
        queued = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex",
            json={},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        assert started.wait(timeout=2)
        try:
            added = client.post(
                f"/v1/knowledge/collections/{collection_id}/documents",
                json={"title": "delta", "text": "delta content"},
            )
            assert added.status_code == 201
            deleting = client.delete(f"/v1/knowledge/documents/{document_id}")
            assert deleting.status_code == 202
            deleted = _poll_deletion_until_terminal(
                client, deleting.json()["operation_id"]
            )
            assert deleted["status"] == "deleted"
            assert (
                client.get(f"/v1/knowledge/indexing-jobs/{job_id}").json()["status"]
                == "running"
            )
        finally:
            release.set()
        final = _poll_until_terminal(client, job_id)
        assert final["status"] == "completed"
        documents_after = client.get(
            f"/v1/knowledge/collections/{collection_id}/documents"
        ).json()["data"]
        assert added.json()["id"] in {item["id"] for item in documents_after}
        assert document_id not in {item["id"] for item in documents_after}


def test_collection_deletion_cancels_active_reindex(monkeypatch) -> None:
    client = make_indexing_client()
    container = client.app.state.container
    service = container.knowledge_service
    assert service is not None
    indexing_service = container.indexing_service
    assert indexing_service is not None
    started = threading.Event()
    release = threading.Event()
    real_reembed = service.reembed_document_with_receipt

    async def blocking_reembed(
        *,
        document,
        embedding_model,
        authority_check=None,
        actor_user_id=None,
        **kwargs,
    ):
        started.set()
        await asyncio.to_thread(release.wait, 2)
        return await real_reembed(
            document=document,
            embedding_model=embedding_model,
            authority_check=authority_check,
            actor_user_id=actor_user_id,
            **kwargs,
        )

    monkeypatch.setattr(
        service,
        "reembed_document_with_receipt",
        blocking_reembed,
    )
    with client:
        collection_id = _collection_with_two_documents(client)
        queued = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex",
            json={},
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        assert started.wait(timeout=2)
        try:
            deleting = client.delete(f"/v1/knowledge/collections/{collection_id}")
            assert deleting.status_code == 202
            deleted = _poll_deletion_until_terminal(
                client, deleting.json()["operation_id"]
            )
            assert deleted["status"] == "deleted"
        finally:
            release.set()
        final = indexing_service.job_store.get(job_id)
        assert final["status"] == "cancelled"
        collections = client.get("/v1/knowledge/collections").json()["data"]
        assert collection_id not in {item["id"] for item in collections}


def test_revoking_edit_share_during_embedding_fails_reindex() -> None:
    class BlockingEmbeddings(StubEmbeddings):
        def __init__(self) -> None:
            super().__init__()
            self.block_reindex = False
            self.started = threading.Event()
            self.release = threading.Event()

        def embed_documents(self, texts, *, model=None):
            if self.block_reindex:
                self.started.set()
                self.release.wait(timeout=3)
            return super().embed_documents(texts, model=model)

    embeddings = BlockingEmbeddings()
    client = make_indexing_client(embeddings=embeddings)
    with client:
        collection_id = _collection_with_two_documents(client)
        granted = client.post(
            "/v1/shares",
            json={
                "resource_type": "knowledge_collection",
                "resource_id": collection_id,
                "invitees": [{"user_id": str(RECIPIENT), "permission": "edit"}],
            },
        )
        assert granted.status_code == 201
        share_id = granted.json()["data"][0]["id"]
        accepted = client.post(
            f"/v1/shares/{share_id}/accept",
            headers=user_headers(RECIPIENT),
        )
        assert accepted.status_code == 200

        embeddings.block_reindex = True
        queued = client.post(
            f"/v1/knowledge/collections/{collection_id}/reindex",
            json={},
            headers=user_headers(RECIPIENT),
        )
        assert queued.status_code == 202
        job_id = queued.json()["job_id"]
        try:
            assert embeddings.started.wait(timeout=2)
            revoked = client.delete(f"/v1/shares/{share_id}")
            assert revoked.status_code == 204
        finally:
            embeddings.release.set()

        final = _poll_until_terminal(client, job_id)
        assert final["status"] == "failed"
        assert final["error"]["type"] == "authorization_revoked"
        assert (
            client.get(
                f"/v1/knowledge/indexing-jobs/{job_id}",
                headers=user_headers(RECIPIENT),
            ).status_code
            == 404
        )

        events = client.get(f"/v1/knowledge/indexing-jobs/{job_id}/events")
        assert "inqtrix.index.failed" in events.text
        assert "inqtrix.index.document_completed" not in events.text
