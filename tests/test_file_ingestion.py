"""End-to-end file-to-knowledge ingestion over the HTTP surface.

Upload through ``/v1/files``, ingest via ``file_id`` into a knowledge
collection (MarkItDown parses server-side), retrieve through search —
plus the access and failure paths: foreign files stay hidden, parses
of binary garbage fail loudly, parser=none rejects clearly.
"""

from __future__ import annotations

import asyncio
import hashlib
from importlib.metadata import version
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.knowledge.parsing import MarkItDownParser
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers import files as files_router
from inqtrix.server.routers import knowledge as knowledge_router
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import Settings, StorageSettings

from tests.contract._app import StubLLM, StubSearch
from tests.fixtures.pdf import build_pdf
from tests.test_knowledge_engine import StubEmbeddings
from tests.test_runs_visibility import SUB_HEADER, HeaderSubAuthProvider

MARKDOWN_PAYLOAD = (
    b"# Rahmenvertrag\n\n"
    b"Die Haftung ist auf den Auftragswert begrenzt.\n"
)


def make_client(tmp_path: Path, *, parser="default") -> TestClient:
    identity = MemoryIdentityStore()
    knowledge_context = KnowledgeProviderContext(
        embeddings=StubEmbeddings(),
        store=MemoryKnowledgeStore(),
        default_top_k=4,
    )
    knowledge_service = KnowledgeService(
        knowledge=knowledge_context,
        chunk_max_chars=2_000,
        max_document_chars=100_000,
        parser=MarkItDownParser() if parser == "default" else None,
    )
    container = build_container(
        providers=ProviderContext(llm=StubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            storage=StorageSettings(
                backend="memory",
                database_url="",
                object_store_path=str(tmp_path / "blobs"),
            )
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=HeaderSubAuthProvider(),
        permissions=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        knowledge=knowledge_context,
    )
    # The container builds its own knowledge service; swap in ours so
    # the parser choice under test is explicit.
    object.__setattr__(container, "knowledge_service", knowledge_service)
    app = FastAPI()
    app.include_router(files_router.build_router(container))
    app.include_router(knowledge_router.build_router(container))
    return TestClient(app)


def upload_and_create_collection(
    client: TestClient,
    *,
    sub: str | None = None,
    content: bytes = MARKDOWN_PAYLOAD,
    file_name: str = "vertrag.md",
) -> tuple[str, str]:
    headers = {SUB_HEADER: sub} if sub else {}
    file_id = client.post(
        "/v1/files",
        files={"file": (file_name, content, "text/markdown")},
        headers=headers,
    ).json()["id"]
    collection_id = client.post(
        "/v1/knowledge/collections", json={"name": "K"}, headers=headers
    ).json()["id"]
    return file_id, collection_id


def test_file_ingestion_end_to_end(tmp_path):
    client = make_client(tmp_path)
    with client:
        file_id, collection_id = upload_and_create_collection(client)

        ingested = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"file_id": file_id},
        )
        assert ingested.status_code == 201, ingested.text
        payload = ingested.json()
        assert payload["title"] == "vertrag.md"
        assert payload["metadata"]["file_id"] == file_id
        assert payload["metadata"]["source_id"] == f"file:{file_id}"
        assert payload["metadata"]["parser"] == "markitdown"

        hits = client.post(
            "/v1/knowledge/search",
            json={"query": "Haftung Auftragswert begrenzt"},
        ).json()["data"]
    assert hits and "Haftung" in hits[0]["excerpt"]


def test_file_ingestion_records_parser_revision_and_the_page_without_text(tmp_path):
    """The document says what produced its text and what that missed."""
    pdf = build_pdf(["Rahmenvertrag, Seite eins.", None, "Anlage, Seite drei."])
    client = make_client(tmp_path)
    with client:
        file_id, collection_id = upload_and_create_collection(
            client, content=pdf, file_name="vertrag.pdf"
        )
        ingested = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={
                "file_id": file_id,
                # Only the server states extraction facts.
                "metadata": {"extraction": {"parser_id": "forged"}, "note": "kept"},
            },
        )
    assert ingested.status_code == 201, ingested.text
    metadata = ingested.json()["metadata"]
    assert metadata["extraction"] == {
        "parser_id": "markitdown",
        "parser_revision": version("markitdown"),
        "file_sha256": hashlib.sha256(pdf).hexdigest(),
        "pages": 3,
        "pages_without_text": [2],
    }
    assert metadata["note"] == "kept"


def test_client_supplied_extraction_facts_are_dropped_from_a_text_document(tmp_path):
    client = make_client(tmp_path)
    with client:
        _, collection_id = upload_and_create_collection(client)
        ingested = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={
                "title": "Notiz",
                "text": "Nur getippter Text.",
                "metadata": {
                    "extraction": {
                        "parser_id": "forged", "pages": 1, "pages_without_text": [],
                    },
                    "note": "kept",
                },
            },
        )
    assert ingested.status_code == 201, ingested.text
    metadata = ingested.json()["metadata"]
    assert "extraction" not in metadata
    assert metadata["note"] == "kept"


def test_foreign_file_stays_hidden_for_ingestion(tmp_path):
    client = make_client(tmp_path)
    with client:
        file_id, _ = upload_and_create_collection(client, sub="user-a")
        collection_id = client.post(
            "/v1/knowledge/collections",
            json={"name": "B"},
            headers={SUB_HEADER: "user-b"},
        ).json()["id"]

        denied = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"file_id": file_id},
            headers={SUB_HEADER: "user-b"},
        )
    assert denied.status_code == 404
    assert denied.json()["error"]["message"] == "File not found"


def test_unparseable_file_fails_loudly(tmp_path):
    client = make_client(tmp_path)
    with client:
        file_id, collection_id = upload_and_create_collection(
            client, content=b"\x00\x01\x02garbage", file_name="kaputt.pdf"
        )
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"file_id": file_id},
        )
    assert response.status_code == 422
    assert "kaputt.pdf" in response.json()["error"]["message"]


def test_text_and_file_id_together_are_rejected(tmp_path):
    client = make_client(tmp_path)
    with client:
        file_id, collection_id = upload_and_create_collection(client)
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"file_id": file_id, "text": "auch noch Text"},
        )
    assert response.status_code == 400


def test_parser_none_rejects_file_ingestion_clearly(tmp_path):
    client = make_client(tmp_path, parser="none")
    with client:
        file_id, collection_id = upload_and_create_collection(client)
        response = client.post(
            f"/v1/knowledge/collections/{collection_id}/documents",
            json={"file_id": file_id},
        )
    assert response.status_code == 400
    assert "INQTRIX_DOCUMENT_PARSER" in response.json()["error"]["message"]


def test_docx_roundtrip_through_real_converter(tmp_path):
    """One real non-trivial format: build a DOCX in-test, parse it."""
    import io
    import zipfile

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>Die Verpflegungspauschale betraegt 28 Euro."
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType='
        '"application/vnd.openxmlformats-officedocument.wordprocessingml.'
        'document.main+xml"/></Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships"><Relationship Id="rId1" Type='
        '"http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document_xml)

    text = MarkItDownParser().parse(
        file_name="reise.docx", content=buffer.getvalue()
    )
    assert "Verpflegungspauschale" in text
