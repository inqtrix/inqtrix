"""Native knowledge retrieval (RAG) over user-provided documents.

Documents are parsed, chunked and embedded at ingestion, and answering
runs through the same algorithm registry as web research. Storage sits
behind the ``KnowledgeStore`` port, which three stores implement: the
in-process store (exact cosine search) as the zero-infrastructure
default, a standalone hybrid Qdrant store, and the Postgres-canonical
store whose vectors live in a ``VectorIndex`` (Qdrant or in-process).
Reranking and the durable indexing worker plug in beside them, so the
algorithm and HTTP surface do not depend on which store runs.

Hard rule: a collection's embedding model (and its dimension) is fixed
at creation. Mixing models inside one collection is rejected loudly —
never padded or truncated.
"""

from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext

__all__ = ["KnowledgeAlgorithm", "KnowledgeProviderContext"]
