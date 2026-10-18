"""Add durable editor-collaboration storage and share contracts.

Revision ID: 0048_editor_collaboration
Revises: 0047_resource_sync

The binary Yjs journal is the durable body authority for collaboration-mode
documents. Markdown remains the last confirmed projection. The collaboration
service never receives database credentials, so every table in this revision
uses the existing application role and forced tenant RLS boundary.

The table definitions below are a frozen revision-local snapshot. Foreign
keys to editor, identity, and auth tables are emitted as raw DDL because those
parents belong to separate historical ``MetaData`` snapshots.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_editor_collaboration"
down_revision = "0047_resource_sync"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"

_LEGACY_SHARE_RESOURCE_TYPES = (
    "'run', 'knowledge_collection', 'prompt_template', 'skill_template'"
)
_SHARE_RESOURCE_TYPES = (
    "'run', 'knowledge_collection', 'prompt_template', 'skill_template', "
    "'editor_document'"
)

_collaboration_metadata = sa.MetaData()

editor_collaboration_updates = sa.Table(
    "editor_collaboration_updates",
    _collaboration_metadata,
    sa.Column("document_id", sa.Text, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("sequence", sa.BigInteger, nullable=False),
    sa.Column(
        "tenant_id",
        sa.Text,
        nullable=False,
        server_default=sa.text("'default'"),
    ),
    sa.Column("update_hash", sa.Text, nullable=False),
    sa.Column("update_bytes", sa.LargeBinary, nullable=True),
    sa.Column(
        "actor_user_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ),
    sa.Column("actor_kind", sa.Text, nullable=False),
    sa.Column("change_kind", sa.Text, nullable=False),
    sa.Column(
        "suggestion_ids",
        postgresql.JSONB,
        nullable=False,
        server_default=sa.text("'[]'::jsonb"),
    ),
    sa.Column(
        "command_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ),
    sa.Column("command_payload_hash", sa.Text, nullable=True),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("payload_pruned_at", sa.Float, nullable=True),
    # Sequences are allocated per document while its row is locked. A global
    # SQL sequence would violate that ordering contract and needs no grant.
    sa.PrimaryKeyConstraint(
        "document_id",
        "generation",
        "sequence",
        name="pk_editor_collaboration_updates",
    ),
    sa.CheckConstraint(
        "generation >= 1 AND sequence >= 1",
        name="ck_collaboration_updates_position",
    ),
    sa.CheckConstraint(
        "length(btrim(update_hash)) > 0",
        name="ck_collaboration_updates_hash",
    ),
    sa.CheckConstraint(
        "actor_kind IN ('human', 'assistant', 'agent', 'system')",
        name="ck_collaboration_updates_actor_kind",
    ),
    sa.CheckConstraint(
        "change_kind IN ('direct', 'suggestion', 'decision', 'system')",
        name="ck_collaboration_updates_change_kind",
    ),
    sa.CheckConstraint(
        "(command_id IS NULL AND command_payload_hash IS NULL) OR "
        "(command_id IS NOT NULL AND length(command_payload_hash) = 64)",
        name="ck_collaboration_updates_command_payload",
    ),
    sa.CheckConstraint(
        "actor_kind <> 'human' OR actor_user_id IS NOT NULL",
        name="ck_collaboration_updates_human_actor",
    ),
    sa.CheckConstraint(
        "jsonb_typeof(suggestion_ids) = 'array'",
        name="ck_collaboration_updates_suggestion_ids",
    ),
    sa.CheckConstraint(
        "(update_bytes IS NOT NULL AND payload_pruned_at IS NULL) OR "
        "(update_bytes IS NULL AND payload_pruned_at IS NOT NULL)",
        name="ck_collaboration_updates_payload",
    ),
    sa.UniqueConstraint(
        "document_id",
        "generation",
        "update_hash",
        name="uq_collaboration_updates_document_hash",
    ),
    sa.Index(
        "ix_collaboration_updates_tenant_document",
        "tenant_id",
        "document_id",
        "generation",
        "sequence",
    ),
    sa.Index(
        "uq_collaboration_updates_command",
        "command_id",
        unique=True,
        postgresql_where=sa.text("command_id IS NOT NULL"),
    ),
    sa.Index(
        "ix_collaboration_updates_actor_user",
        "actor_user_id",
        postgresql_where=sa.text("actor_user_id IS NOT NULL"),
    ),
)

editor_collaboration_snapshots = sa.Table(
    "editor_collaboration_snapshots",
    _collaboration_metadata,
    sa.Column("document_id", sa.Text, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("covered_sequence", sa.BigInteger, nullable=False),
    sa.Column(
        "tenant_id",
        sa.Text,
        nullable=False,
        server_default=sa.text("'default'"),
    ),
    sa.Column("state_update", sa.LargeBinary, nullable=False),
    sa.Column("state_vector", sa.LargeBinary, nullable=False),
    sa.Column("state_hash", sa.Text, nullable=False),
    sa.Column("projection_hash", sa.Text, nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("schema_hash", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.PrimaryKeyConstraint(
        "document_id",
        "generation",
        "covered_sequence",
        name="pk_editor_collaboration_snapshots",
    ),
    sa.CheckConstraint(
        "generation >= 1 AND covered_sequence >= 0",
        name="ck_collaboration_snapshots_position",
    ),
    sa.CheckConstraint(
        "schema_version >= 1 AND length(btrim(schema_hash)) > 0",
        name="ck_collaboration_snapshots_schema",
    ),
    sa.CheckConstraint(
        "length(btrim(state_hash)) > 0 "
        "AND length(btrim(projection_hash)) > 0",
        name="ck_collaboration_snapshots_hashes",
    ),
    sa.Index(
        "ix_collaboration_snapshots_tenant_document",
        "tenant_id",
        "document_id",
        "generation",
        "covered_sequence",
    ),
)

editor_collaboration_leases = sa.Table(
    "editor_collaboration_leases",
    _collaboration_metadata,
    sa.Column(
        "lease_id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    ),
    sa.Column(
        "tenant_id",
        sa.Text,
        nullable=False,
        server_default=sa.text("'default'"),
    ),
    sa.Column("token_hash", sa.Text, nullable=False),
    sa.Column("document_id", sa.Text, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column(
        "user_id",
        postgresql.UUID(as_uuid=True),
        nullable=False,
    ),
    sa.Column("permission", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("issued_at", sa.Float, nullable=False),
    sa.Column("expires_at", sa.Float, nullable=False),
    sa.Column("validated_at", sa.Float, nullable=True),
    sa.Column("revoked_at", sa.Float, nullable=True),
    sa.Column(
        "rotation_command_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ),
    sa.Column(
        "rotated_from_lease_id",
        postgresql.UUID(as_uuid=True),
        nullable=True,
    ),
    sa.CheckConstraint(
        "generation >= 1",
        name="ck_collaboration_leases_generation",
    ),
    sa.CheckConstraint(
        "permission IN ('view', 'suggest', 'edit')",
        name="ck_collaboration_leases_permission",
    ),
    sa.CheckConstraint(
        "length(btrim(token_hash)) > 0 AND expires_at > issued_at",
        name="ck_collaboration_leases_lifetime",
    ),
    sa.CheckConstraint(
        "(validated_at IS NULL OR validated_at >= issued_at) AND "
        "(revoked_at IS NULL OR revoked_at >= issued_at)",
        name="ck_collaboration_leases_timestamps",
    ),
    sa.CheckConstraint(
        "(rotation_command_id IS NULL AND rotated_from_lease_id IS NULL) OR "
        "(rotation_command_id IS NOT NULL AND rotated_from_lease_id IS NOT NULL)",
        name="ck_collaboration_leases_rotation",
    ),
    sa.UniqueConstraint(
        "token_hash",
        name="uq_editor_collaboration_leases_token_hash",
    ),
    sa.UniqueConstraint(
        "tenant_id",
        "rotation_command_id",
        name="uq_collaboration_leases_rotation_command",
    ),
    sa.Index(
        "ix_collaboration_leases_document_user",
        "tenant_id",
        "document_id",
        "generation",
        "user_id",
        postgresql_where=sa.text("revoked_at IS NULL"),
    ),
    sa.Index(
        "ix_collaboration_leases_expiry",
        "tenant_id",
        "expires_at",
        postgresql_where=sa.text("revoked_at IS NULL"),
    ),
    sa.Index("ix_collaboration_leases_user", "user_id"),
    sa.Index("ix_collaboration_leases_session", "session_id"),
    sa.Index("ix_collaboration_leases_rotated_from", "rotated_from_lease_id"),
)

editor_collaboration_instances = sa.Table(
    "editor_collaboration_instances",
    _collaboration_metadata,
    sa.Column(
        "slot",
        sa.Text,
        server_default=sa.text("'primary'"),
    ),
    sa.Column(
        "tenant_id",
        sa.Text,
        nullable=False,
        server_default=sa.text("'default'"),
    ),
    sa.Column("instance_id", sa.Text, nullable=False),
    sa.Column("epoch", sa.BigInteger, nullable=False),
    sa.Column("lease_expires_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.PrimaryKeyConstraint(
        "tenant_id",
        "slot",
        name="pk_editor_collaboration_instances",
    ),
    sa.CheckConstraint(
        "slot = 'primary'",
        name="ck_collaboration_instances_primary_slot",
    ),
    sa.CheckConstraint(
        "length(btrim(instance_id)) > 0 AND epoch >= 1",
        name="ck_collaboration_instances_identity",
    ),
    sa.CheckConstraint(
        "lease_expires_at >= updated_at",
        name="ck_collaboration_instances_lease",
    ),
)

_COLLABORATION_TABLES = (
    "editor_collaboration_updates",
    "editor_collaboration_snapshots",
    "editor_collaboration_leases",
    "editor_collaboration_instances",
)

_OWNER_MAINTENANCE_TABLES = (
    "auth_sessions",
    "editor_comments",
    "editor_documents",
    "editor_patches",
    "resource_shares",
    "users",
)

_DOCUMENT_UPGRADE_SQL = (
    "ALTER TABLE editor_documents ADD COLUMN content_mode text NOT NULL "
    "DEFAULT 'markdown'",
    "ALTER TABLE editor_documents ADD COLUMN metadata_revision bigint "
    "NOT NULL DEFAULT 1",
    "ALTER TABLE editor_documents ADD COLUMN collaboration_generation "
    "bigint NOT NULL DEFAULT 0",
    "ALTER TABLE editor_documents ADD COLUMN collaboration_schema_version "
    "integer NULL",
    "ALTER TABLE editor_documents ADD COLUMN collaboration_schema_hash text NULL",
    "ALTER TABLE editor_documents ADD COLUMN persisted_sequence bigint "
    "NOT NULL DEFAULT 0",
    "ALTER TABLE editor_documents ADD COLUMN projection_sequence bigint "
    "NOT NULL DEFAULT 0",
    "ALTER TABLE editor_documents ADD COLUMN projection_updated_at "
    "double precision NULL",
    "ALTER TABLE editor_documents ADD COLUMN deleted_at double precision NULL",
    "ALTER TABLE editor_documents ADD CONSTRAINT "
    "uq_editor_documents_tenant_document UNIQUE (tenant_id, id)",
    "ALTER TABLE editor_documents ADD CONSTRAINT ck_editor_documents_content_mode "
    "CHECK (content_mode IN ('markdown', 'collaboration'))",
    "ALTER TABLE editor_documents ADD CONSTRAINT ck_editor_documents_metadata_revision "
    "CHECK (metadata_revision >= 1)",
    "ALTER TABLE editor_documents ADD CONSTRAINT "
    "ck_editor_documents_projection_sequence "
    "CHECK (projection_sequence <= persisted_sequence)",
    "ALTER TABLE editor_documents ADD CONSTRAINT "
    "ck_editor_documents_collaboration_state "
    "CHECK ((content_mode = 'markdown' "
    "AND collaboration_generation = 0 "
    "AND collaboration_schema_version IS NULL "
    "AND collaboration_schema_hash IS NULL "
    "AND persisted_sequence = 0 "
    "AND projection_sequence = 0 "
    "AND projection_updated_at IS NULL) "
    "OR (content_mode = 'collaboration' "
    "AND collaboration_generation >= 1 "
    "AND collaboration_schema_version IS NOT NULL "
    "AND collaboration_schema_version >= 1 "
    "AND collaboration_schema_hash IS NOT NULL "
    "AND length(btrim(collaboration_schema_hash)) > 0 "
    "AND persisted_sequence >= 0 "
    "AND projection_sequence >= 0))",
    "CREATE INDEX ix_editor_documents_collaboration_mode ON editor_documents "
    "(tenant_id, content_mode, deleted_at, id)",
)

_PATCH_UPGRADE_SQL = (
    "ALTER TABLE editor_patches ADD COLUMN collaboration_generation bigint NULL",
    "ALTER TABLE editor_patches ADD COLUMN base_sequence bigint NULL",
    "ALTER TABLE editor_patches ADD COLUMN decision_sequence bigint NULL",
    "ALTER TABLE editor_patches ADD COLUMN suggestion_ids jsonb NOT NULL "
    "DEFAULT '[]'::jsonb",
    "ALTER TABLE editor_patches ADD COLUMN decided_by_user_id uuid NULL",
    "ALTER TABLE editor_patches ADD COLUMN command_id uuid NULL",
    "ALTER TABLE editor_patches DROP CONSTRAINT ck_editor_patches_source",
    "ALTER TABLE editor_patches ADD CONSTRAINT ck_editor_patches_source "
    "CHECK (source IN ('suggest', 'instruct', 'agent', 'human'))",
    "ALTER TABLE editor_patches ADD CONSTRAINT ck_editor_patches_collaboration_state "
    "CHECK ((collaboration_generation IS NULL "
    "AND base_sequence IS NULL AND decision_sequence IS NULL) "
    "OR (collaboration_generation IS NOT NULL "
    "AND collaboration_generation >= 1 "
    "AND base_sequence IS NOT NULL AND base_sequence >= 0 "
    "AND (decision_sequence IS NULL OR decision_sequence >= 1)))",
    "ALTER TABLE editor_patches ADD CONSTRAINT ck_editor_patches_suggestion_ids "
    "CHECK (jsonb_typeof(suggestion_ids) = 'array')",
    "ALTER TABLE editor_patches ADD CONSTRAINT fk_editor_patches_decided_by_user "
    "FOREIGN KEY (decided_by_user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "CREATE INDEX ix_editor_patches_collaboration_command ON editor_patches "
    "(tenant_id, document_id, collaboration_generation, command_id) "
    "WHERE command_id IS NOT NULL",
    "CREATE INDEX ix_editor_patches_decided_by_user ON editor_patches "
    "(decided_by_user_id) WHERE decided_by_user_id IS NOT NULL",
)

_COMMENT_PREPARE_SQL = (
    "ALTER TABLE editor_comments ADD COLUMN created_by_user_id uuid NULL",
)

_COMMENT_BACKFILL_SQL = (
    "UPDATE editor_comments AS comment "
    "SET created_by_user_id = document.created_by_user_id "
    "FROM editor_documents AS document "
    "WHERE document.id = comment.document_id "
    "AND document.tenant_id = comment.tenant_id "
    "AND comment.created_by_user_id IS NULL"
)

_COMMENT_FINALIZE_SQL = (
    "ALTER TABLE editor_comments ADD CONSTRAINT fk_editor_comments_created_by_user "
    "FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "CREATE INDEX ix_editor_comments_private_created ON editor_comments "
    "(tenant_id, document_id, created_by_user_id, created_at, id)",
    "CREATE INDEX ix_editor_comments_created_by_user ON editor_comments "
    "(created_by_user_id) WHERE created_by_user_id IS NOT NULL",
)

_COMMENT_UPGRADE_SQL = (
    *_COMMENT_PREPARE_SQL,
    _COMMENT_BACKFILL_SQL,
    *_COMMENT_FINALIZE_SQL,
)

_SHARE_UPGRADE_SQL = (
    "ALTER TABLE resource_shares DROP CONSTRAINT ck_resource_shares_permission",
    "ALTER TABLE resource_shares DROP CONSTRAINT ck_resource_shares_type",
    "ALTER TABLE resource_shares ADD CONSTRAINT ck_resource_shares_type "
    f"CHECK (resource_type IN ({_SHARE_RESOURCE_TYPES}))",
    "ALTER TABLE resource_shares ADD CONSTRAINT ck_resource_shares_permission "
    "CHECK ((resource_type IN "
    f"({_LEGACY_SHARE_RESOURCE_TYPES}) AND permission IN ('view', 'edit')) "
    "OR (resource_type = 'editor_document' "
    "AND permission IN ('view', 'suggest', 'edit')))",
)

_CROSS_METADATA_FK_SQL = (
    "ALTER TABLE editor_collaboration_updates "
    "ADD CONSTRAINT fk_collaboration_updates_document "
    "FOREIGN KEY (tenant_id, document_id) "
    "REFERENCES editor_documents(tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE editor_collaboration_updates "
    "ADD CONSTRAINT fk_collaboration_updates_actor_user "
    "FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE editor_collaboration_snapshots "
    "ADD CONSTRAINT fk_collaboration_snapshots_document "
    "FOREIGN KEY (tenant_id, document_id) "
    "REFERENCES editor_documents(tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE editor_collaboration_leases "
    "ADD CONSTRAINT fk_collaboration_leases_document "
    "FOREIGN KEY (tenant_id, document_id) "
    "REFERENCES editor_documents(tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE editor_collaboration_leases "
    "ADD CONSTRAINT fk_collaboration_leases_user "
    "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT",
    "ALTER TABLE editor_collaboration_leases "
    "ADD CONSTRAINT fk_collaboration_leases_session "
    "FOREIGN KEY (session_id) REFERENCES auth_sessions(id) ON DELETE CASCADE",
)

_SHARE_DOWNGRADE_SQL = (
    "ALTER TABLE resource_shares DROP CONSTRAINT ck_resource_shares_permission",
    "ALTER TABLE resource_shares DROP CONSTRAINT ck_resource_shares_type",
    "DELETE FROM resource_shares WHERE resource_type = 'editor_document' "
    "OR permission = 'suggest'",
    "ALTER TABLE resource_shares ADD CONSTRAINT ck_resource_shares_type "
    f"CHECK (resource_type IN ({_LEGACY_SHARE_RESOURCE_TYPES}))",
    "ALTER TABLE resource_shares ADD CONSTRAINT ck_resource_shares_permission "
    "CHECK (permission IN ('view', 'edit'))",
)

_COMMENT_DOWNGRADE_SQL = (
    "DROP INDEX ix_editor_comments_created_by_user",
    "DROP INDEX ix_editor_comments_private_created",
    "ALTER TABLE editor_comments DROP CONSTRAINT fk_editor_comments_created_by_user",
    "ALTER TABLE editor_comments DROP COLUMN created_by_user_id",
)

_PATCH_DOWNGRADE_SQL = (
    "DROP INDEX ix_editor_patches_decided_by_user",
    "DROP INDEX ix_editor_patches_collaboration_command",
    "ALTER TABLE editor_patches DROP CONSTRAINT fk_editor_patches_decided_by_user",
    "ALTER TABLE editor_patches DROP CONSTRAINT ck_editor_patches_suggestion_ids",
    "ALTER TABLE editor_patches DROP CONSTRAINT ck_editor_patches_collaboration_state",
    "ALTER TABLE editor_patches DROP CONSTRAINT ck_editor_patches_source",
    "UPDATE editor_patches SET source = 'suggest' WHERE source = 'human'",
    "ALTER TABLE editor_patches ADD CONSTRAINT ck_editor_patches_source "
    "CHECK (source IN ('suggest', 'instruct', 'agent'))",
    "ALTER TABLE editor_patches DROP COLUMN command_id",
    "ALTER TABLE editor_patches DROP COLUMN decided_by_user_id",
    "ALTER TABLE editor_patches DROP COLUMN suggestion_ids",
    "ALTER TABLE editor_patches DROP COLUMN decision_sequence",
    "ALTER TABLE editor_patches DROP COLUMN base_sequence",
    "ALTER TABLE editor_patches DROP COLUMN collaboration_generation",
)

_DOCUMENT_DOWNGRADE_SQL = (
    "DROP INDEX ix_editor_documents_collaboration_mode",
    "ALTER TABLE editor_documents DROP CONSTRAINT "
    "ck_editor_documents_collaboration_state",
    "ALTER TABLE editor_documents DROP CONSTRAINT "
    "ck_editor_documents_projection_sequence",
    "ALTER TABLE editor_documents DROP CONSTRAINT "
    "ck_editor_documents_metadata_revision",
    "ALTER TABLE editor_documents DROP CONSTRAINT ck_editor_documents_content_mode",
    "ALTER TABLE editor_documents DROP CONSTRAINT "
    "uq_editor_documents_tenant_document",
    "ALTER TABLE editor_documents DROP COLUMN deleted_at",
    "ALTER TABLE editor_documents DROP COLUMN projection_updated_at",
    "ALTER TABLE editor_documents DROP COLUMN projection_sequence",
    "ALTER TABLE editor_documents DROP COLUMN persisted_sequence",
    "ALTER TABLE editor_documents DROP COLUMN collaboration_schema_hash",
    "ALTER TABLE editor_documents DROP COLUMN collaboration_schema_version",
    "ALTER TABLE editor_documents DROP COLUMN collaboration_generation",
    "ALTER TABLE editor_documents DROP COLUMN metadata_revision",
    "ALTER TABLE editor_documents DROP COLUMN content_mode",
)

_DOWNGRADE_PREFLIGHT_SQL = """
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM editor_documents
        WHERE content_mode = 'collaboration'
          AND projection_sequence <> persisted_sequence
    ) THEN
        RAISE EXCEPTION
            '0048 refuses downgrade while collaboration projections are stale';
    END IF;
END
$migration$;
"""


def _install_tenant_security(table: str) -> None:
    """Grant DML and install the standard fail-closed tenant policy."""
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
        "USING (tenant_id = (SELECT inqtrix_current_tenant_id())) "
        "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))"
    )


def _begin_owner_rls_maintenance(tables: tuple[str, ...]) -> None:
    """Open one transaction-scoped owner maintenance boundary.

    PostgreSQL applies forced row-level security even to a table owner. Alembic
    migrations do not carry a tenant GUC because their backfills, constraint
    validation, and preflights must inspect every tenant. The migration
    therefore takes access-exclusive locks and temporarily removes only
    ``FORCE`` (not RLS or its policy). ``row_security = off`` is a fail-closed
    guard: if the migration login is not the table owner, PostgreSQL raises
    instead of silently operating on a tenant-filtered subset.

    PostgreSQL transactional DDL restores the original forced state if any
    statement fails. The locks are held until Alembic commits, so the app role
    cannot observe or use the owner-only maintenance window.
    """
    if not tables:
        raise ValueError("RLS maintenance requires tables")
    op.execute(f"LOCK TABLE {', '.join(tables)} IN ACCESS EXCLUSIVE MODE")
    for table in tables:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute("SET LOCAL row_security = off")


def _end_owner_rls_maintenance(tables: tuple[str, ...]) -> None:
    """Restore forced RLS before the migration transaction may commit."""
    if not tables:
        raise ValueError("RLS maintenance requires tables")
    for table in tables:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    """Install the collaboration schema without changing legacy body writes."""
    _begin_owner_rls_maintenance(_OWNER_MAINTENANCE_TABLES)
    for statement in _DOCUMENT_UPGRADE_SQL:
        op.execute(statement)
    for statement in _PATCH_UPGRADE_SQL:
        op.execute(statement)
    for statement in _COMMENT_PREPARE_SQL:
        op.execute(statement)
    op.execute(_COMMENT_BACKFILL_SQL)
    for statement in _COMMENT_FINALIZE_SQL:
        op.execute(statement)
    for statement in _SHARE_UPGRADE_SQL:
        op.execute(statement)

    _collaboration_metadata.create_all(bind=op.get_bind(), checkfirst=False)
    for statement in _CROSS_METADATA_FK_SQL:
        op.execute(statement)
    for table in _COLLABORATION_TABLES:
        _install_tenant_security(table)
    _end_owner_rls_maintenance(_OWNER_MAINTENANCE_TABLES)


def downgrade() -> None:
    """Remove collaboration storage and restore the revision-0047 contracts."""
    _begin_owner_rls_maintenance(_OWNER_MAINTENANCE_TABLES)
    op.execute(_DOWNGRADE_PREFLIGHT_SQL)
    for statement in _SHARE_DOWNGRADE_SQL:
        op.execute(statement)
    for statement in _COMMENT_DOWNGRADE_SQL:
        op.execute(statement)
    for statement in _PATCH_DOWNGRADE_SQL:
        op.execute(statement)

    _collaboration_metadata.drop_all(bind=op.get_bind(), checkfirst=False)

    for statement in _DOCUMENT_DOWNGRADE_SQL:
        op.execute(statement)
    _end_owner_rls_maintenance(_OWNER_MAINTENANCE_TABLES)
