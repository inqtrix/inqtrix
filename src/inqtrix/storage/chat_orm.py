"""SQLAlchemy Core definitions of the chat-history persistence schema.

Part of the project-persistence tier: when the platform runs with
``INQTRIX_STORAGE_BACKEND=postgres``, a user's chat threads, their
grouping, and their messages become server-persistent instead of living
only in the local markdown project. Scoped per ``(tenant_id,
created_by_user_id, workspace_id)`` exactly like ``runs`` / ``files`` /
``knowledge_collections`` — a user's project is "their data in the
current workspace", the decided one-project-per-(user, workspace) model.
No separate ``projects`` table: the workspace is the project container
(``project_id`` can be added additively later for multi-project).

Separate ``MetaData`` from the other domain schemas on purpose (its
migration is an immutable snapshot, like ``knowledge_metadata``). The
editor and asset/index schemas get their own ORM modules so each stays a
self-contained migration.

Type decisions (mirroring ``runs_orm.py`` / ``knowledge_orm.py``):

* Ids are text with the client-supplied public prefixes (``ct_``/
  ``ctg_``/``cm_``), used verbatim as the primary key so the wire
  surface stays byte-identical and autosave is an idempotent upsert by
  id (no local-vs-server id mapping layer).
* Timestamps are unix-seconds doubles (``Float``), matching every other
  table here and the frontend ``ChatThreadRecord`` ISO timestamps once
  the client adapter converts.
* ``chat_thread_groups`` membership is a nullable ``group_id`` column on
  the thread (the local ``chatThreadGroupMemberships`` dict is exactly a
  thread->group map); a separate membership table would be redundant.
* The message's rich optional fields (``attachments``/``chainTrace``/
  ``modelResolution``) are stored verbatim in a text-preserving ``JSON``
  ``metadata`` column (NOT ``JSONB``) so a loaded message re-serializes
  to the document the client supplied — never reinterpreted.
* Every table carries ``tenant_id`` for the established row-level
  security layering (GRANT + ENABLE/FORCE RLS + fail-closed tenant
  policy in the migration, like ``0010_knowledge``).
* Keyset-pagination indexes carry the ``id`` tiebreaker (the float-epoch
  ``created_at`` can collide), consistent with ``0012_keyset_indexes``.
"""

from __future__ import annotations

from sqlalchemy import (
    Column,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID

chat_metadata = MetaData()

chat_thread_groups = Table(
    "chat_thread_groups",
    chat_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Index(
        "ix_chat_thread_groups_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
        "id",
    ),
)
"""Optional grouping of a user's chat threads. ``created_by_user_id`` is the
ownership anchor (``None`` = unscoped/anonymous deployments, the single
implicit owner). Listed by ``created_at`` (groups are few — no keyset
page), the id keeps the index unique for the tiebreaker convention."""

chat_threads = Table(
    "chat_threads",
    chat_metadata,
    Column("id", Text, primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("workspace_id", Text, nullable=True),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("title_revision", Integer, nullable=False, server_default=text("0")),
    Column("title_source", Text, nullable=False, server_default=text("'manual'")),
    CheckConstraint("title_revision >= 0", name="ck_chat_threads_title_revision"),
    CheckConstraint("title_source IN ('initial', 'generated', 'manual')", name="ck_chat_threads_title_source"),
    Column("preview", Text, nullable=False, server_default=text("''")),
    Column("source", Text, nullable=False, server_default=text("'api'")),
    Column(
        "group_id",
        Text,
        ForeignKey("chat_thread_groups.id", ondelete="SET NULL"),
        nullable=True,
    ),
    # The model picked inside this thread, as client-owned JSON (the
    # ``items_json`` pattern): the client normalizes defensively on read,
    # the server only length-caps. '' means "nothing picked here".
    Column("model_selection", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    # Keyset-pagination index for the owner-scoped thread list (newest
    # first), with the id tiebreaker. List sort is by created_at (stable)
    # — NOT updated_at, which mutates on every message and would shift
    # the cursor under the reader (the frontend re-sorts by updated_at
    # for display).
    Index(
        "ix_chat_threads_owner_created",
        "tenant_id",
        "created_by_user_id",
        "created_at",
        "id",
    ),
)
"""One chat conversation. ``source`` (``api``/``imported``/``mock``) and
``group_id`` round-trip the local ``ChatThreadRecord``; ``preview`` is
the denormalized last-line shown in the thread list. Deleting a group
orphans its threads to ungrouped (``ON DELETE SET NULL``), never deletes
the conversations."""

chat_messages = Table(
    "chat_messages",
    chat_metadata,
    # COMPOSITE primary key (thread_id, id): a message's identity is
    # scoped to its thread, not global. With the client message id as the
    # sole PK, an autosave into thread B could silently overwrite a
    # same-id message that lives in thread A (an isolation break) — a
    # conflict on (thread_id, id) can only ever touch a row already in the
    # caller's own thread, so a cross-thread id reuse inserts a fresh
    # owned row instead of hijacking another user's message.
    Column("id", Text, primary_key=True),
    Column(
        "thread_id",
        Text,
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("role", Text, nullable=False),
    Column("content_markdown", Text, nullable=False, server_default=text("''")),
    Column("metadata", JSON, nullable=False, server_default=text("'{}'")),
    Column("created_at", Float, nullable=False),
    # Keyset-pagination index within a thread (oldest first when read;
    # the conversation order). created_at is stable for messages (they
    # are not re-timestamped), so the cursor is gap-free.
    Index("ix_chat_messages_thread_created", "thread_id", "created_at", "id"),
)
"""One message in a thread. ``content_markdown`` is the rendered body;
``metadata`` holds the verbatim optional client fields (``attachments``,
``chainTrace``, ``modelResolution``) so a round-trip reconstructs the
exact ``ChatMessageRecord``. Visibility inherits from the parent thread
(documents-inherit-from-collection pattern). Cascades on thread delete."""
