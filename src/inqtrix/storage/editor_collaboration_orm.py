"""SQLAlchemy Core schema for durable editor collaboration.

The migration is the frozen DDL authority. These runtime tables mirror its
columns so the FastAPI collaboration store can participate in the existing
tenant-scoped transaction and RLS discipline.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

editor_collaboration_metadata = MetaData()

editor_collaboration_updates = Table(
    "editor_collaboration_updates",
    editor_collaboration_metadata,
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("sequence", BigInteger, nullable=False),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("update_hash", Text, nullable=False),
    Column("update_bytes", LargeBinary, nullable=True),
    Column("actor_user_id", UUID(as_uuid=True), nullable=True),
    Column("actor_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("actor_kind", Text, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column(
        "suggestion_ids",
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
    Column(
        "change_summary",
        JSONB,
        nullable=False,
        server_default=text(
            """'{"edits":[],"omitted_edit_count":0}'::jsonb"""
        ),
    ),
    Column("decision_outcome", Text, nullable=True),
    Column("command_id", UUID(as_uuid=True), nullable=True),
    Column("command_payload_hash", Text, nullable=True),
    Column("created_at", Float, nullable=False),
    Column("payload_pruned_at", Float, nullable=True),
    # Suggestions this update removed under an edit lease, each removed by
    # the update's actor; the audit trail keeps who proposed each one.
    Column(
        "rejected_suggestion_ids",
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
    PrimaryKeyConstraint(
        "document_id",
        "generation",
        "sequence",
        name="pk_editor_collaboration_updates",
    ),
    CheckConstraint(
        "generation >= 1 AND sequence >= 1",
        name="ck_collaboration_updates_position",
    ),
    CheckConstraint(
        "length(btrim(update_hash)) > 0",
        name="ck_collaboration_updates_hash",
    ),
    CheckConstraint(
        "(actor_kind = 'human' AND actor_user_id IS NOT NULL "
        "AND actor_guest_identity_id IS NULL) OR "
        "(actor_kind = 'guest' AND actor_user_id IS NULL "
        "AND actor_guest_identity_id IS NOT NULL) OR "
        "(actor_kind IN ('assistant', 'agent', 'system') "
        "AND actor_guest_identity_id IS NULL)",
        name="ck_collaboration_updates_actor",
    ),
    CheckConstraint(
        "jsonb_typeof(suggestion_ids) = 'array'",
        name="ck_collaboration_updates_suggestion_ids",
    ),
    CheckConstraint(
        "jsonb_typeof(change_summary) = 'object'",
        name="ck_collaboration_updates_change_summary",
    ),
    CheckConstraint(
        "decision_outcome IS NULL OR "
        "decision_outcome IN ('accepted', 'rejected')",
        name="ck_collaboration_updates_decision_outcome",
    ),
    CheckConstraint(
        "(update_bytes IS NOT NULL AND payload_pruned_at IS NULL) OR "
        "(update_bytes IS NULL AND payload_pruned_at IS NOT NULL)",
        name="ck_collaboration_updates_payload",
    ),
    CheckConstraint(
        "actor_kind IN ('human', 'guest', 'assistant', 'agent', 'system')",
        name="ck_collaboration_updates_actor_kind",
    ),
    CheckConstraint(
        "change_kind IN ('direct', 'suggestion', 'decision', 'system')",
        name="ck_collaboration_updates_change_kind",
    ),
    CheckConstraint(
        "(command_id IS NULL AND command_payload_hash IS NULL) OR "
        "(command_id IS NOT NULL AND length(command_payload_hash) = 64)",
        name="ck_collaboration_updates_command_payload",
    ),
    CheckConstraint(
        "jsonb_typeof(rejected_suggestion_ids) = 'array'",
        name="ck_collaboration_updates_rejected_suggestion_ids",
    ),
    CheckConstraint(
        "rejected_suggestion_ids = '[]'::jsonb OR ("
        "decision_outcome IS NULL "
        "AND change_kind IN ('direct', 'suggestion') "
        "AND actor_kind IN ('human', 'guest'))",
        name="ck_collaboration_updates_rejected_by_edit",
    ),
    UniqueConstraint(
        "document_id",
        "generation",
        "update_hash",
        name="uq_collaboration_updates_document_hash",
    ),
    Index(
        "ix_collaboration_updates_tenant_document",
        "tenant_id",
        "document_id",
        "generation",
        "sequence",
    ),
    Index(
        "uq_collaboration_updates_command",
        "command_id",
        unique=True,
        postgresql_where=text("command_id IS NOT NULL"),
    ),
    Index(
        "ix_collaboration_updates_actor_user",
        "actor_user_id",
        postgresql_where=text("actor_user_id IS NOT NULL"),
    ),
    Index(
        "ix_collaboration_updates_actor_guest",
        "actor_guest_identity_id",
        postgresql_where=text("actor_guest_identity_id IS NOT NULL"),
    ),
)

editor_collaboration_snapshots = Table(
    "editor_collaboration_snapshots",
    editor_collaboration_metadata,
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("covered_sequence", BigInteger, nullable=False),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("state_update", LargeBinary, nullable=False),
    Column("state_vector", LargeBinary, nullable=False),
    Column("state_hash", Text, nullable=False),
    Column("projection_hash", Text, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("schema_hash", Text, nullable=False),
    Column("created_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "document_id",
        "generation",
        "covered_sequence",
        name="pk_editor_collaboration_snapshots",
    ),
    CheckConstraint(
        "generation >= 1 AND covered_sequence >= 0",
        name="ck_collaboration_snapshots_position",
    ),
    CheckConstraint(
        "schema_version >= 1 AND length(btrim(schema_hash)) > 0",
        name="ck_collaboration_snapshots_schema",
    ),
    CheckConstraint(
        "length(btrim(state_hash)) > 0 AND length(btrim(projection_hash)) > 0",
        name="ck_collaboration_snapshots_hashes",
    ),
    Index(
        "ix_collaboration_snapshots_tenant_document",
        "tenant_id",
        "document_id",
        "generation",
        "covered_sequence",
    ),
)

editor_collaboration_leases = Table(
    "editor_collaboration_leases",
    editor_collaboration_metadata,
    Column(
        "lease_id",
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    ),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("token_hash", Text, nullable=False),
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=True),
    Column("actor_kind", Text, nullable=False, server_default=text("'user'")),
    Column("guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("guest_link_id", UUID(as_uuid=True), nullable=True),
    Column("permission", Text, nullable=False),
    Column("session_id", Text, nullable=True),
    Column("issued_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("validated_at", Float, nullable=True),
    Column("revoked_at", Float, nullable=True),
    Column("rotation_command_id", UUID(as_uuid=True), nullable=True),
    Column("rotated_from_lease_id", UUID(as_uuid=True), nullable=True),
    CheckConstraint(
        "generation >= 1",
        name="ck_collaboration_leases_generation",
    ),
    CheckConstraint(
        "permission IN ('view', 'suggest', 'edit')",
        name="ck_collaboration_leases_permission",
    ),
    CheckConstraint(
        "length(btrim(token_hash)) > 0 AND expires_at > issued_at",
        name="ck_collaboration_leases_lifetime",
    ),
    CheckConstraint(
        "(validated_at IS NULL OR validated_at >= issued_at) AND "
        "(revoked_at IS NULL OR revoked_at >= issued_at)",
        name="ck_collaboration_leases_timestamps",
    ),
    CheckConstraint(
        "(rotation_command_id IS NULL AND rotated_from_lease_id IS NULL) OR "
        "(rotation_command_id IS NOT NULL AND rotated_from_lease_id IS NOT NULL)",
        name="ck_collaboration_leases_rotation",
    ),
    CheckConstraint(
        "(actor_kind = 'user' AND user_id IS NOT NULL "
        "AND guest_identity_id IS NULL AND guest_link_id IS NULL "
        "AND session_id IS NOT NULL) OR "
        "(actor_kind = 'guest' AND user_id IS NULL "
        "AND guest_identity_id IS NOT NULL AND guest_link_id IS NOT NULL "
        "AND session_id IS NULL)",
        name="ck_collaboration_leases_actor",
    ),
    UniqueConstraint(
        "token_hash",
        name="uq_editor_collaboration_leases_token_hash",
    ),
    UniqueConstraint(
        "tenant_id",
        "rotation_command_id",
        name="uq_collaboration_leases_rotation_command",
    ),
    Index(
        "ix_collaboration_leases_document_user",
        "tenant_id",
        "document_id",
        "generation",
        "user_id",
        postgresql_where=text("revoked_at IS NULL"),
    ),
    Index(
        "ix_collaboration_leases_expiry",
        "tenant_id",
        "expires_at",
        postgresql_where=text("revoked_at IS NULL"),
    ),
    Index("ix_collaboration_leases_user", "user_id"),
    Index(
        "ix_collaboration_leases_guest_identity",
        "guest_identity_id",
        postgresql_where=text("guest_identity_id IS NOT NULL"),
    ),
    Index("ix_collaboration_leases_session", "session_id"),
    Index("ix_collaboration_leases_rotated_from", "rotated_from_lease_id"),
)

editor_collaboration_instances = Table(
    "editor_collaboration_instances",
    editor_collaboration_metadata,
    Column("slot", Text, server_default=text("'primary'")),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("instance_id", Text, nullable=False),
    Column("epoch", BigInteger, nullable=False),
    Column("lease_expires_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "slot",
        name="pk_editor_collaboration_instances",
    ),
    CheckConstraint(
        "slot = 'primary'",
        name="ck_collaboration_instances_primary_slot",
    ),
    CheckConstraint(
        "length(btrim(instance_id)) > 0 AND epoch >= 1",
        name="ck_collaboration_instances_identity",
    ),
    CheckConstraint(
        "lease_expires_at >= updated_at",
        name="ck_collaboration_instances_lease",
    ),
)

editor_collaboration_comment_threads = Table(
    "editor_collaboration_comment_threads",
    editor_collaboration_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'open'")),
    Column("created_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("created_by_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("resolved_by_user_id", UUID(as_uuid=True), nullable=True),
    Column("resolved_by_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("resolved_at", Float, nullable=True),
    Column("anchor", JSONB, nullable=False),
    Column("quote_text", Text, nullable=False, server_default=text("''")),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("last_command_id", UUID(as_uuid=True), nullable=False),
    Column("last_command_payload_hash", Text, nullable=False),
    Column("last_command_kind", Text, nullable=False),
    # A deleted thread keeps its row and its messages for the audit trail and
    # disappears from every read path -- the card is gone for the people in
    # the document, the record is not.
    Column("deleted_at", Float, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "id",
        name="uq_collaboration_comment_threads_tenant_id",
    ),
    CheckConstraint(
        "generation >= 1 AND revision >= 1",
        name="ck_collaboration_comment_threads_position",
    ),
    CheckConstraint(
        "status IN ('open', 'resolved')",
        name="ck_collaboration_comment_threads_status",
    ),
    CheckConstraint(
        "jsonb_typeof(anchor) = 'object'",
        name="ck_collaboration_comment_threads_anchor",
    ),
    CheckConstraint(
        "length(last_command_payload_hash) = 64",
        name="ck_collaboration_comment_threads_command_hash",
    ),
    CheckConstraint(
        "last_command_kind IN ('create', 'resolve', 'reopen', 'delete')",
        name="ck_collaboration_comment_threads_command_kind",
    ),
    CheckConstraint(
        "(created_by_user_id IS NOT NULL)::integer "
        "+ (created_by_guest_identity_id IS NOT NULL)::integer = 1",
        name="ck_collaboration_comment_threads_creator_actor",
    ),
    CheckConstraint(
        "(status = 'open' AND resolved_by_user_id IS NULL "
        "AND resolved_by_guest_identity_id IS NULL AND resolved_at IS NULL) "
        "OR (status = 'resolved' AND "
        "((resolved_by_user_id IS NOT NULL)::integer "
        "+ (resolved_by_guest_identity_id IS NOT NULL)::integer = 1) "
        "AND resolved_at IS NOT NULL)",
        name="ck_collaboration_comment_threads_resolution",
    ),
    Index(
        "ix_collaboration_comment_threads_document_revision",
        "tenant_id",
        "document_id",
        "generation",
        "revision",
    ),
    Index(
        "ix_collaboration_comment_threads_status_updated",
        "tenant_id",
        "document_id",
        "generation",
        "status",
        "updated_at",
    ),
)

editor_collaboration_comment_messages = Table(
    "editor_collaboration_comment_messages",
    editor_collaboration_metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("document_id", Text, nullable=False),
    Column("thread_id", UUID(as_uuid=True), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("author_user_id", UUID(as_uuid=True), nullable=True),
    Column("author_guest_identity_id", UUID(as_uuid=True), nullable=True),
    Column("body_markdown", Text, nullable=False),
    Column(
        "mention_user_ids",
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    ),
    Column("created_at", Float, nullable=False),
    Column("edited_at", Float, nullable=True),
    Column("deleted_at", Float, nullable=True),
    Column("last_command_id", UUID(as_uuid=True), nullable=False),
    Column("last_command_payload_hash", Text, nullable=False),
    Column("last_command_kind", Text, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "thread_id"],
        [
            "editor_collaboration_comment_threads.tenant_id",
            "editor_collaboration_comment_threads.id",
        ],
        ondelete="CASCADE",
        name="fk_collaboration_comment_messages_thread",
    ),
    CheckConstraint(
        "revision >= 1",
        name="ck_collaboration_comment_messages_revision",
    ),
    CheckConstraint(
        "(author_user_id IS NOT NULL)::integer "
        "+ (author_guest_identity_id IS NOT NULL)::integer = 1",
        name="ck_collaboration_comment_messages_author_actor",
    ),
    CheckConstraint(
        "jsonb_typeof(mention_user_ids) = 'array'",
        name="ck_collaboration_comment_messages_mentions",
    ),
    CheckConstraint(
        "length(last_command_payload_hash) = 64",
        name="ck_collaboration_comment_messages_command_hash",
    ),
    CheckConstraint(
        "last_command_kind IN ('create', 'reply', 'edit', 'delete')",
        name="ck_collaboration_comment_messages_command_kind",
    ),
    Index(
        "ix_collaboration_comment_messages_thread_created",
        "tenant_id",
        "thread_id",
        "created_at",
        "id",
    ),
    Index(
        "ix_collaboration_comment_messages_document_revision",
        "tenant_id",
        "document_id",
        "revision",
    ),
)

editor_collaboration_comment_reads = Table(
    "editor_collaboration_comment_reads",
    editor_collaboration_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("document_id", Text, nullable=False),
    Column("generation", BigInteger, nullable=False),
    Column("user_id", UUID(as_uuid=True), nullable=False),
    Column(
        "last_read_revision",
        BigInteger,
        nullable=False,
        server_default=text("0"),
    ),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint(
        "tenant_id",
        "document_id",
        "generation",
        "user_id",
        name="pk_editor_collaboration_comment_reads",
    ),
    CheckConstraint(
        "generation >= 1 AND last_read_revision >= 0",
        name="ck_collaboration_comment_reads_position",
    ),
)
