"""Retrieval collapses content-identical documents to one contributor.

Deduplication at ingest is bound to the source: two files with different
names are two documents by design, so an identical copy is embedded twice
and then fills several evidence slots with the same passages. The fix sits
at retrieval, where "which documents support this answer" is actually
asked.

Document-level on purpose: two DIFFERENT documents quoting the same clause
both stay visible, because the fact that both say it is itself information.
"""

from __future__ import annotations

import pytest

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.services.knowledge_service import (
    KnowledgeProviderContext,
    KnowledgeService,
)

from tests.test_knowledge_engine import StubEmbeddings

CLAUSE = "Die Erstmeldung erfolgt binnen zwei Stunden ab Kenntnisnahme."


def make_service() -> KnowledgeService:
    identity = MemoryIdentityStore()
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=10,
        ),
        authorization=AuthorizationService(
            members=identity, shares=identity, audit=identity
        ),
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )


@pytest.mark.asyncio
async def test_identical_copy_contributes_once():
    """The same content under two file names must not double its slots."""
    service = make_service()
    collection = await service.create_collection(name="Meldewege")
    for title in ("meldewege.md", "meldewege-kopie.md"):
        await service.add_document(
            collection_id=collection.id, title=title, text=CLAUSE
        )

    hits = await service.search(query="Erstmeldung Kenntnisnahme")

    assert hits, "the corpus must be findable at all"
    assert len({hit.chunk.document_id for hit in hits}) == 1


@pytest.mark.asyncio
async def test_different_documents_sharing_a_clause_both_stay():
    """Agreement between distinct sources is evidence, not redundancy."""
    service = make_service()
    collection = await service.create_collection(name="Richtlinien")
    await service.add_document(
        collection_id=collection.id,
        title="richtlinie-a.md",
        text=f"{CLAUSE} Ergaenzung A zur Eskalation des Krisenstabs.",
    )
    await service.add_document(
        collection_id=collection.id,
        title="richtlinie-b.md",
        text=f"{CLAUSE} Ergaenzung B zur Abgrenzung der Meldewege.",
    )

    hits = await service.search(query="Erstmeldung Kenntnisnahme")

    assert len({hit.chunk.document_id for hit in hits}) == 2
