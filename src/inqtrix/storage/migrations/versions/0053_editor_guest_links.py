"""Add secure editor guest links and guest collaboration actors.

Revision ID: 0053_editor_guest_links
Revises: 0052_editor_review

Tokens remain outside the database; only keyed SHA-256 digests and Argon2id
password hashes are persisted. Guest identities are document- and link-bound,
which makes revocation effective for both HTTP requests and live leases.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053_editor_guest_links"
down_revision = "0052_editor_review"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
TABLES = (
    "editor_document_share_links",
    "editor_document_guest_identities",
)


def _install_tenant_security(table: str) -> None:
    op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC, {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}"
    )
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} FOR ALL "
        "USING (tenant_id = (SELECT inqtrix_current_tenant_id())) "
        "WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))"
    )


def upgrade() -> None:
    op.create_table(
        "editor_document_share_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("permission", sa.Text(), nullable=False),
        sa.Column("token_digest", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column(
            "successful_open_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "session_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_accessed_at", sa.Float(), nullable=True),
        sa.Column(
            "last_command_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("last_command_payload_hash", sa.Text(), nullable=False),
        sa.Column("last_command_kind", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_editor_document_share_links",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_editor_document_share_links_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "token_digest",
            name="uq_editor_document_share_links_token_digest",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "last_command_id",
            name="uq_editor_document_share_links_command",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND revision >= 1",
            name="ck_editor_document_share_links_position",
        ),
        sa.CheckConstraint(
            "permission IN ('view', 'comment', 'suggest', 'edit')",
            name="ck_editor_document_share_links_permission",
        ),
        sa.CheckConstraint(
            "length(token_digest) = 64 "
            "AND length(btrim(password_hash)) > 0 "
            "AND length(btrim(label)) BETWEEN 2 AND 24",
            name="ck_editor_document_share_links_secrets",
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND updated_at >= created_at "
            "AND (revoked_at IS NULL OR revoked_at >= created_at)",
            name="ck_editor_document_share_links_timestamps",
        ),
        sa.CheckConstraint(
            "successful_open_count >= 0 AND session_count >= 0",
            name="ck_editor_document_share_links_stats",
        ),
        sa.CheckConstraint(
            "length(last_command_payload_hash) = 64",
            name="ck_editor_document_share_links_command_hash",
        ),
        sa.CheckConstraint(
            "last_command_kind IN "
            "('create', 'update', 'revoke', 'rotate_password')",
            name="ck_editor_document_share_links_command_kind",
        ),
    )
    op.create_index(
        "ix_editor_document_share_links_document",
        "editor_document_share_links",
        ["tenant_id", "document_id", "created_at", "id"],
    )
    op.create_index(
        "ix_editor_document_share_links_expiry",
        "editor_document_share_links",
        ["tenant_id", "expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_foreign_key(
        "fk_editor_document_share_links_document",
        "editor_document_share_links",
        "editor_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_editor_document_share_links_creator",
        "editor_document_share_links",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "editor_document_guest_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "link_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("document_id", sa.Text(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("session_token_digest", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_seen_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("revoked_at", sa.Float(), nullable=True),
        sa.Column(
            "open_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "last_read_revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_editor_document_guest_identities",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            name="uq_editor_document_guest_identities_tenant_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "session_token_digest",
            name="uq_editor_document_guest_identities_session",
        ),
        sa.CheckConstraint(
            "generation >= 1 AND open_count >= 1 AND last_read_revision >= 0",
            name="ck_editor_document_guest_identities_position",
        ),
        sa.CheckConstraint(
            "display_name IS NULL OR length(btrim(display_name)) BETWEEN 1 AND 80",
            name="ck_editor_document_guest_identities_name",
        ),
        sa.CheckConstraint(
            "length(session_token_digest) = 64",
            name="ck_editor_document_guest_identities_token",
        ),
        sa.CheckConstraint(
            "last_seen_at >= created_at AND expires_at > created_at "
            "AND (revoked_at IS NULL OR revoked_at >= created_at)",
            name="ck_editor_document_guest_identities_timestamps",
        ),
    )
    op.create_index(
        "ix_editor_document_guest_identities_link",
        "editor_document_guest_identities",
        ["tenant_id", "link_id", "last_seen_at"],
    )
    op.create_index(
        "ix_editor_document_guest_identities_document",
        "editor_document_guest_identities",
        ["tenant_id", "document_id", "last_seen_at"],
    )
    op.create_foreign_key(
        "fk_editor_document_guest_identities_link",
        "editor_document_guest_identities",
        "editor_document_share_links",
        ["tenant_id", "link_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_editor_document_guest_identities_document",
        "editor_document_guest_identities",
        "editor_documents",
        ["tenant_id", "document_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )

    op.add_column(
        "editor_collaboration_leases",
        sa.Column(
            "actor_kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
    )
    op.add_column(
        "editor_collaboration_leases",
        sa.Column(
            "guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "editor_collaboration_leases",
        sa.Column(
            "guest_link_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.alter_column(
        "editor_collaboration_leases",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        "(actor_kind = 'user' AND user_id IS NOT NULL "
        "AND guest_identity_id IS NULL AND guest_link_id IS NULL) OR "
        "(actor_kind = 'guest' AND user_id IS NULL "
        "AND guest_identity_id IS NOT NULL AND guest_link_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_collaboration_leases_guest_identity",
        "editor_collaboration_leases",
        "editor_document_guest_identities",
        ["tenant_id", "guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_collaboration_leases_guest_link",
        "editor_collaboration_leases",
        "editor_document_share_links",
        ["tenant_id", "guest_link_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_collaboration_leases_guest_identity",
        "editor_collaboration_leases",
        ["guest_identity_id"],
        postgresql_where=sa.text("guest_identity_id IS NOT NULL"),
    )

    op.drop_constraint(
        "ck_collaboration_updates_human_actor",
        "editor_collaboration_updates",
        type_="check",
    )
    op.drop_constraint(
        "ck_collaboration_updates_actor_kind",
        "editor_collaboration_updates",
        type_="check",
    )
    op.add_column(
        "editor_collaboration_updates",
        sa.Column(
            "actor_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_collaboration_updates_actor",
        "editor_collaboration_updates",
        "(actor_kind = 'human' AND actor_user_id IS NOT NULL "
        "AND actor_guest_identity_id IS NULL) OR "
        "(actor_kind = 'guest' AND actor_user_id IS NULL "
        "AND actor_guest_identity_id IS NOT NULL) OR "
        "(actor_kind IN ('assistant', 'agent', 'system') "
        "AND actor_guest_identity_id IS NULL)",
    )
    op.create_check_constraint(
        "ck_collaboration_updates_actor_kind",
        "editor_collaboration_updates",
        "actor_kind IN ('human', 'guest', 'assistant', 'agent', 'system')",
    )
    op.create_foreign_key(
        "fk_collaboration_updates_guest_actor",
        "editor_collaboration_updates",
        "editor_document_guest_identities",
        ["tenant_id", "actor_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_collaboration_updates_actor_guest",
        "editor_collaboration_updates",
        ["actor_guest_identity_id"],
        postgresql_where=sa.text("actor_guest_identity_id IS NOT NULL"),
    )

    op.drop_constraint(
        "fk_collaboration_comment_threads_creator",
        "editor_collaboration_comment_threads",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_collaboration_comment_threads_resolver",
        "editor_collaboration_comment_threads",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_collaboration_comment_threads_resolution",
        "editor_collaboration_comment_threads",
        type_="check",
    )
    op.alter_column(
        "editor_collaboration_comment_threads",
        "created_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "editor_collaboration_comment_threads",
        sa.Column(
            "created_by_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "editor_collaboration_comment_threads",
        sa.Column(
            "resolved_by_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_collaboration_comment_threads_creator_actor",
        "editor_collaboration_comment_threads",
        "(created_by_user_id IS NOT NULL)::integer "
        "+ (created_by_guest_identity_id IS NOT NULL)::integer = 1",
    )
    op.create_check_constraint(
        "ck_collaboration_comment_threads_resolution",
        "editor_collaboration_comment_threads",
        "(status = 'open' AND resolved_by_user_id IS NULL "
        "AND resolved_by_guest_identity_id IS NULL AND resolved_at IS NULL) "
        "OR (status = 'resolved' AND "
        "((resolved_by_user_id IS NOT NULL)::integer "
        "+ (resolved_by_guest_identity_id IS NOT NULL)::integer = 1) "
        "AND resolved_at IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_creator",
        "editor_collaboration_comment_threads",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_resolver",
        "editor_collaboration_comment_threads",
        "users",
        ["resolved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_guest_creator",
        "editor_collaboration_comment_threads",
        "editor_document_guest_identities",
        ["tenant_id", "created_by_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_guest_resolver",
        "editor_collaboration_comment_threads",
        "editor_document_guest_identities",
        ["tenant_id", "resolved_by_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(
        "fk_collaboration_comment_messages_author",
        "editor_collaboration_comment_messages",
        type_="foreignkey",
    )
    op.alter_column(
        "editor_collaboration_comment_messages",
        "author_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "editor_collaboration_comment_messages",
        sa.Column(
            "author_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_collaboration_comment_messages_author_actor",
        "editor_collaboration_comment_messages",
        "(author_user_id IS NOT NULL)::integer "
        "+ (author_guest_identity_id IS NOT NULL)::integer = 1",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_messages_author",
        "editor_collaboration_comment_messages",
        "users",
        ["author_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_messages_guest_author",
        "editor_collaboration_comment_messages",
        "editor_document_guest_identities",
        ["tenant_id", "author_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "editor_patches",
        sa.Column(
            "created_by_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "editor_patches",
        sa.Column(
            "decided_by_guest_identity_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_editor_patches_created_by_guest",
        "editor_patches",
        "editor_document_guest_identities",
        ["tenant_id", "created_by_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_editor_patches_decided_by_guest",
        "editor_patches",
        "editor_document_guest_identities",
        ["tenant_id", "decided_by_guest_identity_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )

    # PostgreSQL validates the incoming collaboration foreign keys against
    # these new relations. Install FORCE RLS only after every such constraint
    # exists; the complete revision remains one managed transaction.
    for table in TABLES:
        _install_tenant_security(table)


def downgrade() -> None:
    op.drop_constraint(
        "fk_editor_patches_decided_by_guest",
        "editor_patches",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_editor_patches_created_by_guest",
        "editor_patches",
        type_="foreignkey",
    )
    op.drop_column("editor_patches", "decided_by_guest_identity_id")
    op.drop_column("editor_patches", "created_by_guest_identity_id")

    op.drop_constraint(
        "fk_collaboration_comment_messages_guest_author",
        "editor_collaboration_comment_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_collaboration_comment_messages_author",
        "editor_collaboration_comment_messages",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_collaboration_comment_messages_author_actor",
        "editor_collaboration_comment_messages",
        type_="check",
    )
    op.drop_column(
        "editor_collaboration_comment_messages",
        "author_guest_identity_id",
    )
    op.alter_column(
        "editor_collaboration_comment_messages",
        "author_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_collaboration_comment_messages_author",
        "editor_collaboration_comment_messages",
        "users",
        ["author_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    for constraint in (
        "fk_collaboration_comment_threads_guest_resolver",
        "fk_collaboration_comment_threads_guest_creator",
        "fk_collaboration_comment_threads_resolver",
        "fk_collaboration_comment_threads_creator",
    ):
        op.drop_constraint(
            constraint,
            "editor_collaboration_comment_threads",
            type_="foreignkey",
        )
    op.drop_constraint(
        "ck_collaboration_comment_threads_resolution",
        "editor_collaboration_comment_threads",
        type_="check",
    )
    op.drop_constraint(
        "ck_collaboration_comment_threads_creator_actor",
        "editor_collaboration_comment_threads",
        type_="check",
    )
    op.drop_column(
        "editor_collaboration_comment_threads",
        "resolved_by_guest_identity_id",
    )
    op.drop_column(
        "editor_collaboration_comment_threads",
        "created_by_guest_identity_id",
    )
    op.alter_column(
        "editor_collaboration_comment_threads",
        "created_by_user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_collaboration_comment_threads_resolution",
        "editor_collaboration_comment_threads",
        "(status = 'open' AND resolved_by_user_id IS NULL "
        "AND resolved_at IS NULL) OR "
        "(status = 'resolved' AND resolved_by_user_id IS NOT NULL "
        "AND resolved_at IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_creator",
        "editor_collaboration_comment_threads",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_collaboration_comment_threads_resolver",
        "editor_collaboration_comment_threads",
        "users",
        ["resolved_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_index(
        "ix_collaboration_updates_actor_guest",
        table_name="editor_collaboration_updates",
    )
    op.drop_constraint(
        "fk_collaboration_updates_guest_actor",
        "editor_collaboration_updates",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_collaboration_updates_actor_kind",
        "editor_collaboration_updates",
        type_="check",
    )
    op.drop_constraint(
        "ck_collaboration_updates_actor",
        "editor_collaboration_updates",
        type_="check",
    )
    op.drop_column(
        "editor_collaboration_updates",
        "actor_guest_identity_id",
    )
    op.create_check_constraint(
        "ck_collaboration_updates_human_actor",
        "editor_collaboration_updates",
        "actor_kind <> 'human' OR actor_user_id IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_collaboration_updates_actor_kind",
        "editor_collaboration_updates",
        "actor_kind IN ('human', 'assistant', 'agent', 'system')",
    )

    op.drop_index(
        "ix_collaboration_leases_guest_identity",
        table_name="editor_collaboration_leases",
    )
    op.drop_constraint(
        "fk_collaboration_leases_guest_link",
        "editor_collaboration_leases",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_collaboration_leases_guest_identity",
        "editor_collaboration_leases",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        type_="check",
    )
    op.execute(
        "DELETE FROM editor_collaboration_leases WHERE actor_kind = 'guest'"
    )
    op.alter_column(
        "editor_collaboration_leases",
        "user_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column("editor_collaboration_leases", "guest_link_id")
    op.drop_column("editor_collaboration_leases", "guest_identity_id")
    op.drop_column("editor_collaboration_leases", "actor_kind")

    for table in reversed(TABLES):
        op.drop_table(table)
