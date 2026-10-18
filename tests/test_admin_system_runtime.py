"""Instance-admin runtime manifest for the system settings panel."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.api_key import build_local_provider
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.admin import build_admin_router
from inqtrix.server.routers.admin_system import build_router
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.services import system_runtime as system_runtime_module
from inqtrix.services.system_runtime import (
    runtime_feature_overrides,
    system_runtime_payload,
    system_runtime_payload_checked,
)
from inqtrix.settings import (
    AuthSettings,
    KnowledgeSettings,
    QueueSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM

OWNER = ("owner@example.com", "correct-horse-battery")


def _configured_runtime_container():
    settings = Settings(
        server=ServerSettings(enable_openapi=False, public_base_url=""),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://user:secret@db/inqtrix",
            object_store_backend="s3",
            s3_endpoint_url="https://s3.secret.example",
            s3_bucket="private-bucket",
            s3_access_key="runtime-test-access",
            s3_secret_key="runtime-test-secret",
        ),
        queue=QueueSettings(
            backend="valkey",
            valkey_url="redis://:secret@valkey:6379/0",
        ),
        knowledge=KnowledgeSettings(
            enabled=True,
            embedding_model="text-embedding-3-large",
            embedding_provider="azure",
            sparse="bm25_german",
            vector_backend="qdrant",
        ),
    )
    knowledge_service = SimpleNamespace(
        knowledge=SimpleNamespace(
            contextualizer=object(),
            default_top_k=8,
            embeddings=SimpleNamespace(default_model="text-embedding-3-large"),
            reranker=object(),
            store=SimpleNamespace(supports_hybrid=True, sparse_language="de"),
        ),
        parser=object(),
    )
    return SimpleNamespace(
        # Deliberately DIVERGENT from the settings default (4): the
        # payload must publish the handle's EFFECTIVE value, never the
        # configured knob -- a disabled/volatile deployment has no pool.
        agent_checkpointer=SimpleNamespace(max_connections=7, durable=True),
        file_service=SimpleNamespace(text_extraction_available=True),
        knowledge_service=knowledge_service,
        object_store_backend="s3",
        settings=settings,
    )


class _UnavailableFileService:
    text_extraction_available = True

    async def object_store_available(self) -> bool:
        return False


class _UnavailableKnowledgeStore:
    supports_hybrid = True

    async def is_available(self) -> bool:
        return False


def _unreachable_runtime_container():
    container = _configured_runtime_container()
    container.file_service = _UnavailableFileService()
    container.knowledge_service.knowledge.store = _UnavailableKnowledgeStore()
    return container


def test_runtime_payload_reports_configured_backends_without_secrets():
    payload = system_runtime_payload(_configured_runtime_container())

    assert payload["storage"] == {"backend": "postgres", "durable": True}
    # One source, three displays: the payload reads the HANDLE (7),
    # not the configured knob (settings default 4) -- effective over
    # configured, exactly like the startup budget line.
    assert payload["agents"] == {"checkpointer_pool_size": 7}
    assert payload["runs"] == {
        "execution": "worker_dispatch",
        "queue": "valkey",
        "queue_available": True,
        "queue_consumers": None,
        "queue_depth": None,
        "store": "postgres",
        "worker_dispatch": True,
        # Admission limits of this api process; execution capacity belongs
        # to the worker fleet in worker_dispatch mode and is not claimed.
        "admission_max_concurrent": 100,
        "queue_max_size": 100,
    }
    assert payload["files"]["object_store"] == "s3"
    assert payload["files"]["blob_storage"] == "s3"
    assert payload["files"]["object_store_available"] is True
    assert payload["knowledge"]["default_top_k"] == 8
    assert payload["knowledge"]["embedding_provider"] == "azure"
    assert payload["knowledge"]["vector_store"] == "qdrant"
    assert payload["knowledge"]["vector_store_available"] is True
    assert payload["knowledge"]["hybrid_retrieval"] is True
    # Cross-lingual honesty: BM25 is monolingual ("de" here) and never
    # cross-lingual; the lever is a multilingual reranker. The store delegates
    # the tokenizer language up from the Qdrant vector index.
    assert payload["knowledge"]["sparse"] == "bm25_german"
    assert payload["knowledge"]["sparse_mode"] == "bm25"
    assert payload["knowledge"]["sparse_language"] == "de"
    assert payload["knowledge"]["sparse_multilingual"] is False
    assert payload["knowledge"]["cross_lingual_recommendation"] == "reranker"
    assert payload["api"]["openapi"] is False
    # Effective values from this process's settings -- the admin panel's
    # source for "what is actually enforced here".
    assert payload["api"]["chat_max_concurrent"] == 100
    assert payload["api"]["stream_reader_workers"] == 128
    # Tracing block: mode off ⇒ inactive, no retention, no deep link;
    # never any endpoint or credential. content_capture follows the
    # ambient OBSERVABILITY_PROFILE (env-driven in this fixture), so it
    # is asserted for presence and type rather than a fixed value.
    block = dict(payload["observability"])
    assert isinstance(block.pop("content_capture"), bool)
    assert block == {
        "tracing": "off",
        "tracing_active": False,
        "sample_rate": 1.0,
        "spool": False,
        "retention_days": None,
        # Whether ANY process runs the prune jobs: they live in the
        # worker, so a worker-less deployment keeps rows forever no
        # matter what the retention days say.
        "retention_enforced": True,
        "ui_link_configured": False,
    }

    serialized = json.dumps(payload, sort_keys=True)
    assert "secret" not in serialized
    assert "postgresql" not in serialized
    assert "s3.secret.example" not in serialized
    assert "private-bucket" not in serialized


def test_runtime_payload_observability_reports_configured_vs_effective():
    from inqtrix.settings import ObservabilitySettings

    container = _configured_runtime_container()
    container.settings.observability = ObservabilitySettings(
        tracing="otlp",
        trace_retention_days=14,
        trace_ui_url="http://localhost:3300",
    )
    payload = system_runtime_payload(container)
    block = payload["observability"]
    assert block["tracing"] == "otlp"
    # No provider is installed in this test process: configured and
    # effective must diverge visibly.
    assert block["tracing_active"] is False
    assert block["retention_days"] == 14
    assert block["ui_link_configured"] is True

    # retention_days=0 documents "job off" — the status must not
    # advertise a cleanup job with 0-day retention.
    container.settings.observability = ObservabilitySettings(
        tracing="otlp", trace_retention_days=0
    )
    block = system_runtime_payload(container)["observability"]
    assert block["retention_days"] is None


def test_runtime_payload_checked_reports_unreachable_backends(monkeypatch):
    monkeypatch.setattr(system_runtime_module, "_ping_valkey", lambda url: False)

    payload = asyncio.run(
        system_runtime_payload_checked(_unreachable_runtime_container())
    )

    assert payload["files"]["object_store"] == "s3"
    assert payload["files"]["object_store_available"] is False
    assert payload["knowledge"]["vector_store"] == "qdrant"
    assert payload["knowledge"]["vector_store_available"] is False
    assert payload["knowledge"]["hybrid_retrieval"] is False
    assert payload["runs"]["queue"] == "valkey"
    assert payload["runs"]["queue_available"] is False
    assert payload["runs"]["worker_dispatch"] is True

    features = runtime_feature_overrides(payload)
    assert features["files"] is False
    assert features["knowledge"] is False
    assert features["hybrid_retrieval"] is False
    # Document parsing is pure CPU work (MarkItDown) with no vector-store
    # dependency: it stays available even though the vector store is down, so
    # file uploads are not silently downgraded to the weaker client parser.
    assert features["document_parser"] is True


def _default_container(tmp_path, **storage):
    """The container a plain deployment builds: knowledge stays off."""
    settings = Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(
            backend="memory",
            database_url="",
            object_store_backend="local",
            object_store_path=str(tmp_path / "blobs"),
            **storage,
        ),
    )
    return build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
    )


def test_runtime_reports_the_document_parser_with_the_files_while_knowledge_is_off(tmp_path):
    payload = system_runtime_payload(_default_container(tmp_path))

    assert payload["knowledge"]["enabled"] is False
    assert payload["files"]["document_parser"] == "markitdown"
    assert "document_parser" not in payload["knowledge"]
    assert runtime_feature_overrides(payload)["document_parser"] is True


def test_runtime_reports_no_document_parser_when_it_is_switched_off(tmp_path):
    payload = system_runtime_payload(
        _default_container(tmp_path, document_parser="none")
    )

    assert payload["files"]["document_parser"] == "none"
    assert runtime_feature_overrides(payload)["document_parser"] is False


def _client() -> TestClient:
    settings = Settings(
        auth=AuthSettings(
            mode="local",
            oidc_insecure_dev_cookies=True,
            pat_pepper="p" * 32,
            session_secret="s" * 32,
        ),
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
    )
    provider = build_local_provider(settings)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
    )
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_admin_router(provider))
    app.include_router(build_router(container))
    return TestClient(app, base_url="http://127.0.0.1:5100")


def _owner_client():
    client = _client()
    client.post(
        "/api/setup/owner",
        json={"display_name": "Owner", "email": OWNER[0], "password": OWNER[1]},
    )
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    return client, csrf


def test_admin_runtime_endpoint_requires_session_admin():
    assert _client().get("/v1/admin/system/runtime").status_code == 401

    client, csrf = _owner_client()
    ok = client.get("/v1/admin/system/runtime")
    assert ok.status_code == 200
    assert ok.json()["storage"]["backend"] == "memory"
    assert ok.json()["files"]["object_store"] == "local"

    created = client.post(
        "/v1/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"email": "bob@example.com", "password": "another-strong-pw-1"},
    )
    assert created.status_code == 201
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/auth/login/local",
        json={"identifier": "bob@example.com", "password": "another-strong-pw-1"},
    )

    assert client.get("/v1/admin/system/runtime").status_code == 404


def test_bounded_probe_timeout_names_the_bound(monkeypatch, caplog):
    """A probe timeout must not degrade to an empty diagnostic.

    ``str(TimeoutError())`` is empty on Python >= 3.11; the log line has to
    name the bound and point at the backend's own detailed warning.
    """
    import logging

    monkeypatch.setattr(
        system_runtime_module, "_RUNTIME_PROBE_TIMEOUT_SECONDS", 0.05
    )

    async def hanging_probe() -> bool:
        await asyncio.sleep(1.0)
        return True

    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        result = asyncio.run(
            system_runtime_module._bounded_probe("object_store", hanging_probe)
        )
    finally:
        logger.removeHandler(caplog.handler)

    assert result is False
    assert any(
        "object_store" in record.message and "timed out after" in record.message
        for record in caplog.records
    )


def test_bounded_probe_names_exception_type(caplog):
    import logging

    async def broken_probe() -> bool:
        raise ValueError("boom")

    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        result = asyncio.run(
            system_runtime_module._bounded_probe("vector_store", broken_probe)
        )
    finally:
        logger.removeHandler(caplog.handler)

    assert result is False
    assert any(
        "vector_store" in record.message and "ValueError" in record.message
        for record in caplog.records
    )


def test_runtime_payload_carries_probed_queue_consumer_visibility():
    """XINFO-derived consumer/depth values reach the runs manifest."""
    from inqtrix.services.system_runtime import RuntimeProbeResults

    container = _configured_runtime_container()
    probes = RuntimeProbeResults(
        queue_available=True,
        queue_consumers=2,
        queue_depth=5,
    )
    payload = system_runtime_payload(container, probes=probes)
    assert payload["runs"]["queue_consumers"] == 2
    assert payload["runs"]["queue_depth"] == 5

    degraded = system_runtime_payload(
        container,
        probes=RuntimeProbeResults(queue_available=True),
    )
    assert degraded["runs"]["queue_consumers"] is None
    assert degraded["runs"]["queue_depth"] is None


def test_agents_block_hides_the_pool_without_a_durable_checkpointer():
    """No durable handle (disabled agent, missing extra, volatile mode):
    the payload must publish None so the panel row hides, instead of
    naming a psycopg pool that can never open."""
    container = _configured_runtime_container()
    container.agent_checkpointer = None
    assert system_runtime_payload(container)["agents"] == {
        "checkpointer_pool_size": None
    }
    container.agent_checkpointer = SimpleNamespace(
        max_connections=0, durable=False
    )
    assert system_runtime_payload(container)["agents"] == {
        "checkpointer_pool_size": None
    }
