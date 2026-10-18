"""SQLAlchemy Core definitions of the vector-index-record schema.

The vector-index layer of the project-persistence tier: the local file
library's RAG indexes (the file<->collection mapping the user builds for
retrieval), scoped per ``(tenant_id, created_by_user_id, workspace_id)`` like
the chat/editor/asset entities. The vectors themselves live in Qdrant and
the canonical chunk text in the ``knowledge_*`` tables; ``server_collection_id``
references that backend collection. What persists HERE is the client's
index record: its identity, embedding model/dims, lifecycle status, the
n:m membership of files, and the capped run history shown inline.

One record with two owned child collections, both travelling WITH the
record (the serialized ``VectorIndexRecord`` carries them) and therefore
replaced wholesale on each upsert — no per-child endpoint, unlike editor
comments:

* ``vector_index_members``  — the documents referenced by the index
  (composite PK ``(index_id, file_id)``; ``state`` is the only persisted
  lifecycle datum — chunk/vector counts are derived, never stored).
* ``vector_index_history``  — past reindex runs, newest first, capped at
  the frontend's ``VECTOR_INDEX_HISTORY_LIMIT``. History entries have no
  client identity, so their order IS their key: composite PK
  ``(index_id, seq)`` with ``seq`` the 0-based newest-first position.

Type decisions match the chat/editor/asset ORMs: the opaque client-supplied
id (e.g. ``vector-index-...``) as the record PK, unix-seconds ``Float`` timestamps,
per-table ``tenant_id`` for the RLS layering, keyset index with the id
tiebreaker. CHECK constraints pin the status / member state / run result
to the frontend unions (``VectorIndexStatus`` / ``VectorIndexMemberState``
/ ``VectorIndexRunResult``) so an out-of-domain write fails loudly.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

vector_index_metadata = MetaData()

vector_index_records = Table(
    "vector_index_records",
    vector_index_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("handle", Text, nullable=False, server_default=text("''")),
    Column("model", Text, nullable=False),
    Column("dims", Integer, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False, server_default=text("'stale'")),
    # The backend knowledge-collection id once the index was embedded on a
    # connected server; null = a simulated (demo/offline) index.
    Column("server_collection_id", Text, nullable=True),
    # The embedding model the server collection was BUILT with. Lets a reindex
    # tell "documents added" (same model -> incremental ingest) from "model
    # changed" (different -> full rebuild). Must persist across reload, else
    # every post-reload add falls back to a full rebuild.
    Column("server_collection_model", Text, nullable=True),
    # Visible failure message of the last server reindex attempt; null when
    # the last run succeeded (No-Silent-Fallbacks: a failed run must never
    # look like a merely stale index).
    Column("last_error", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_vector_index_records_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
        "id",
    ),
)
"""One vector index (RAG file<->collection mapping). ``members`` and
``history`` live in the child tables. ``created_by_user_id`` is the ownership
anchor (``None`` = unscoped/anonymous deployments)."""

vector_index_members = Table(
    "vector_index_members",
    vector_index_metadata,
    # COMPOSITE primary key (index_id, file_id): a member's identity is its
    # file within the index, never global — an upsert into index B can never
    # touch a same-file row in index A (the chat_messages isolation rule).
    Column(
        "index_id",
        Text,
        ForeignKey("vector_index_records.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("file_id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    # Membership order is user-visible (the frontend renders an index's
    # documents in array order), so it must round-trip deterministically.
    # ``seq`` is the 0-based position in the client's member array; the file
    # within the index is still the identity (the composite PK), unlike
    # history whose order IS its key.
    Column("seq", Integer, nullable=False, server_default=text("0")),
    Column("state", Text, nullable=False, server_default=text("'pending'")),
    # The backend knowledge-document id this member was ingested as, once known.
    # Lets "remove from index" delete the exact document from the searchable
    # collection without a full rebuild. Must persist across reload; a null
    # legacy value is reconciled through stable source identity before the
    # durable deletion starts. Null = ingested before this was tracked / offline.
    Column("server_document_id", Text, nullable=True),
)
"""The documents referenced by an index (n:m). Replaced wholesale on each
record upsert; cascade-deleted with the index. Read back in ``seq`` order
so both store tiers preserve the client's array order. Visibility inherits
from the parent record (owner-scoping lives at the record level)."""

vector_index_history = Table(
    "vector_index_history",
    vector_index_metadata,
    # COMPOSITE primary key (index_id, seq): a history entry has no client
    # identity, so its newest-first position IS its key. The frontend caps
    # the list at VECTOR_INDEX_HISTORY_LIMIT; the record upsert rewrites the
    # whole list, so seq is contiguous from 0.
    Column(
        "index_id",
        Text,
        ForeignKey("vector_index_records.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("seq", Integer, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("result", Text, nullable=False),
    Column("documents", Integer, nullable=False, server_default=text("0")),
    Column("duration_ms", BigInteger, nullable=False, server_default=text("0")),
    Column("error", Text, nullable=True),
    Column("started_at", Float, nullable=False),
    Column("finished_at", Float, nullable=False),
)
"""Past reindex runs of an index, newest first (``seq`` ascending = newest
to oldest). Replaced wholesale on each record upsert; cascade-deleted with
the index."""
