"""SQLAlchemy Core definitions of the canonical knowledge schema.

Separate ``MetaData`` from the other domain schemas on purpose (its
migration is an immutable snapshot, like ``runs_metadata``). This is the
**relational source of truth** for the knowledge engine once
``INQTRIX_STORAGE_BACKEND=postgres`` is active: collections, documents
(full canonical text), and chunk metadata live here; Qdrant holds ONLY
the vectors plus a lean payload (the chunk id and filter keys). The
split follows the standard "Postgres = source of truth, vector DB =
derived index" topology.

Type decisions (mirroring ``runs_orm.py``):

* Ids are text with the existing public prefixes (``kc_``/``kd_``/
  ``kch_``), unchanged from the in-memory and Qdrant stores so the wire
  surface stays byte-identical.
* Timestamps are unix-seconds doubles (``Float``), matching the
  :class:`~inqtrix.knowledge.stores.ports.KnowledgeCollection` /
  ``KnowledgeDocument`` dataclasses and every other table here.
* ``metadata`` uses the text-preserving ``JSON`` type (NOT ``JSONB``) so
  a loaded dict re-serializes to the document the client supplied —
  client metadata is stored verbatim, never reinterpreted.
* ``embedding_dim`` is immutable per collection (set at creation,
  enforced on every upsert); it lives on the collection row.
* ``vector_synced`` on the document is the durable vector-sync
  diagnostic: the canonical text commits to Postgres first, then the
  vectors upsert to Qdrant and the flag flips ``true``; a failed vector
  sync leaves it ``false`` (a queryable, operator-visible "vectors out
  of sync" signal — No Silent Fallbacks), and re-running reindex
  re-embeds from the canonical text and clears it. No outbox table and
  no vector duplication in Postgres. An automatic reconcile sweep keyed
  on the flag is a follow-up. Chunk vectors are never stored here.
* Every table carries ``tenant_id`` for the established row-level
  security layering (GRANT + ENABLE/FORCE RLS + fail-closed tenant
  policy added in the migration, like ``0007_quota``).
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

knowledge_metadata = MetaData()

knowledge_collections = Table(
    "knowledge_collections",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("name", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("embedding_dim", Integer, nullable=False),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("active_generation_id", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("ix_knowledge_collections_tenant_created", "tenant_id", "created_at"),
    UniqueConstraint(
        "tenant_id", "id", name="uq_knowledge_collections_tenant_id"
    ),
)
"""Logical knowledge collections. ``embedding_model``/``embedding_dim``
are immutable after creation; ``created_by_user_id`` is the ownership anchor
(``None`` = legacy/unscoped, visible to all — the established rule). Knowledge
is the cross-workspace SHARING surface (owner + ACL via resource_shares), NOT
per-workspace project data, so there is deliberately NO workspace_id dimension
here (see docs/architecture/data-architecture.md). A dead
always-NULL workspace_id column was dropped in migration 0018."""

knowledge_documents = Table(
    "knowledge_documents",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "collection_id",
        Text,
        ForeignKey("knowledge_collections.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("title", Text, nullable=False),
    Column("text", Text, nullable=False, server_default=text("''")),
    Column("metadata", JSON, nullable=False, server_default=text("'{}'")),
    Column("source_id", Text, nullable=True),
    Column("source_owner_user_id", UUID(as_uuid=True), nullable=True),
    Column("source_workspace_id", Text, nullable=True),
    Column(
        "source_scope_bound",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("desired_revision_id", Text, nullable=True),
    Column("active_revision_id", Text, nullable=True),
    Column("desired_sequence", BigInteger, nullable=False, server_default=text("0")),
    Column(
        "lifecycle_status",
        Text,
        nullable=False,
        server_default=text("'active'"),
    ),
    Column("chunk_count", Integer, nullable=False, server_default=text("0")),
    Column(
        "vector_synced",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    Column("created_at", Float, nullable=False),
    # Keyset-pagination index with the id tiebreaker (created_at is a float
    # epoch that can collide); supersedes (collection_id, created_at).
    Index(
        "ix_knowledge_documents_collection_created_id",
        "collection_id",
        "created_at",
        "id",
    ),
    Index(
        "uq_knowledge_documents_collection_source",
        "collection_id",
        "source_id",
        unique=True,
        postgresql_where=text("source_id IS NOT NULL"),
    ),
    Index("ix_knowledge_documents_source_status", "source_id", "lifecycle_status"),
    Index(
        "ix_knowledge_documents_source_scope_status",
        "tenant_id",
        "source_id",
        "source_owner_user_id",
        "source_workspace_id",
        "lifecycle_status",
    ),
    UniqueConstraint(
        "tenant_id", "id", name="uq_knowledge_documents_tenant_id"
    ),
)
"""Ingested documents with their full canonical ``text`` (the source of
truth retrieval hydrates from and reindex re-embeds from). ``metadata``
is stored verbatim. ``vector_synced`` is the reconcile flag (see module
docstring)."""

knowledge_document_revisions = Table(
    "knowledge_document_revisions",
    knowledge_metadata,
    Column("revision_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("document_id", Text, nullable=False),
    Column("collection_id", Text, nullable=False),
    Column("source_id", Text, nullable=True),
    Column("content_hash", Text, nullable=False),
    Column("build_contract_hash", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("metadata", JSON, nullable=False, server_default=text("'{}'")),
    Column("status", Text, nullable=False, server_default=text("'staging'")),
    Column("created_at", Float, nullable=False),
    Column("activated_at", Float, nullable=True),
    Column("superseded_at", Float, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "collection_id",
        "source_id",
        "content_hash",
        "build_contract_hash",
        name="uq_knowledge_revision_build_identity",
    ),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"],
        ["knowledge_documents.tenant_id", "knowledge_documents.id"],
        name="fk_knowledge_revisions_tenant_document",
        ondelete="CASCADE",
    ),
    Index(
        "ix_knowledge_revisions_document_created",
        "tenant_id",
        "document_id",
        "created_at",
    ),
)
"""Immutable source/build revisions behind the mutable document projection."""

knowledge_index_generations = Table(
    "knowledge_index_generations",
    knowledge_metadata,
    Column("generation_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("collection_id", Text, nullable=False),
    Column("build_contract_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'building'")),
    Column("manifest", JSON, nullable=False, server_default=text("'{}'")),
    Column("validation", JSON, nullable=False, server_default=text("'{}'")),
    Column("created_at", Float, nullable=False),
    Column("activated_at", Float, nullable=True),
    Column("superseded_at", Float, nullable=True),
    Column("rollback_until", Float, nullable=True),
    ForeignKeyConstraint(
        ["tenant_id", "collection_id"],
        ["knowledge_collections.tenant_id", "knowledge_collections.id"],
        name="fk_knowledge_generations_tenant_collection",
        ondelete="CASCADE",
    ),
    Index(
        "ix_knowledge_generations_collection_status",
        "tenant_id",
        "collection_id",
        "status",
        "created_at",
    ),
)
"""Physical index generations retained for bounded atomic rollback."""

knowledge_chunks = Table(
    "knowledge_chunks",
    knowledge_metadata,
    Column("id", Text, primary_key=True),
    Column(
        "document_id",
        Text,
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("collection_id", Text, nullable=False),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("chunk_index", Integer, nullable=False),
    Column("text", Text, nullable=False),
    Column("source_text", Text, nullable=False, server_default=text("''")),
    Column("retrieval_context", Text, nullable=True),
    Column("source_start", BigInteger, nullable=True),
    Column("source_end", BigInteger, nullable=True),
    Column("document_content_hash", Text, nullable=True),
    Column("revision_id", Text, nullable=True),
    Column("generation_id", Text, nullable=True),
    Column("page_number", Integer, nullable=True),
    Column("created_at", Float, nullable=False),
    Index("ix_knowledge_chunks_document_index", "document_id", "chunk_index"),
)
"""Per-chunk metadata: the embedded ``text`` (may carry a
contextualization prefix) and the pre-contextualization ``source_text``
quote verification runs against. The DENSE/sparse VECTORS are NOT stored
here — they live in Qdrant, keyed by this chunk ``id``. Retrieval returns
chunk ids from Qdrant and hydrates text/source_text + document title from
these rows."""
