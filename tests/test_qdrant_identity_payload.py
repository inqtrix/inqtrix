"""Canonical identity contract at the Qdrant JSON payload boundary."""

from __future__ import annotations

import uuid

from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore


def test_collection_payload_restores_canonical_user_uuid() -> None:
    """Qdrant returns JSON strings; ownership must remain UUID-typed."""
    owner_user_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    store = object.__new__(QdrantKnowledgeStore)
    store._count_documents = lambda _collection_id: 0

    collection = store._collection_payload(
        {
            "record_id": "kc_1",
            "name": "Collection",
            "embedding_model": "text-embedding-3-large",
            "embedding_dim": 3072,
            "created_at": 1.0,
            "tenant_id": "default",
            "created_by_user_id": str(owner_user_id),
        }
    )

    assert collection.created_by_user_id == owner_user_id
    assert isinstance(collection.created_by_user_id, uuid.UUID)
