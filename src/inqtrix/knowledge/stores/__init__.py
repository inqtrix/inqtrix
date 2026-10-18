"""Storage ports and implementations for the knowledge engine.

:mod:`~inqtrix.knowledge.stores.ports` defines the contracts;
:mod:`~inqtrix.knowledge.stores.memory` is the in-process default that
keeps the no-infrastructure deployment fully functional. Postgres and
Qdrant implementations join as drop-ins behind the same ports.
"""

from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    DocumentChunk,
    KnowledgeCollection,
    KnowledgeDocument,
    KnowledgeProviderContext,
    KnowledgeStore,
    RetrievalCandidate,
)

__all__ = [
    "DocumentChunk",
    "KnowledgeCollection",
    "KnowledgeDocument",
    "KnowledgeProviderContext",
    "KnowledgeStore",
    "MemoryKnowledgeStore",
    "RetrievalCandidate",
]
