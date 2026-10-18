"""Add source-exact knowledge revisions and resumable indexing metadata.

Revision ID: 0055_knowledge_revision
Revises: 0054_guest_lease_sessions

Retrieval context becomes separate from source evidence, documents gain a
stable source/revision lifecycle, collections gain an active generation
pointer, and indexing jobs can expose phase/batch checkpoints and visible
paused states.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_knowledge_revision"
down_revision = "0054_guest_lease_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_collections",
        sa.Column("active_generation_id", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE knowledge_collections SET active_generation_id = "
        "'gen_legacy_' || substr(md5(id), 1, 20) "
        "WHERE active_generation_id IS NULL"
    )

    for column in (
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("desired_revision_id", sa.Text(), nullable=True),
        sa.Column("active_revision_id", sa.Text(), nullable=True),
        sa.Column(
            "desired_sequence",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "lifecycle_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    ):
        op.add_column("knowledge_documents", column)

    # Legacy source identity exists under both spellings. Keep the most recent
    # copy searchable and quarantine older ambiguous duplicates rather than
    # deleting them during a schema migration.
    op.execute(
        """
        WITH raw_sources AS (
            SELECT id, collection_id, created_at,
                   NULLIF(BTRIM(metadata->>'source_id'), '') AS explicit_source,
                   COALESCE(
                       NULLIF(BTRIM(metadata->>'fileId'), ''),
                       NULLIF(BTRIM(metadata->>'file_id'), '')
                   ) AS legacy_source
            FROM knowledge_documents
        ), normalized AS (
            SELECT id, collection_id, created_at,
                   CASE
                       WHEN explicit_source IS NOT NULL THEN explicit_source
                       WHEN legacy_source LIKE 'asset:%' THEN legacy_source
                       ELSE 'asset:' || legacy_source
                   END AS source_id
            FROM raw_sources
            WHERE explicit_source IS NOT NULL OR legacy_source IS NOT NULL
        ), candidates AS (
            SELECT id, source_id,
                   row_number() OVER (
                       PARTITION BY collection_id, source_id
                       ORDER BY created_at DESC, id DESC
                   ) AS source_rank
            FROM normalized
        )
        UPDATE knowledge_documents AS document
        SET source_id = CASE
                WHEN candidates.source_rank = 1 THEN candidates.source_id
                ELSE NULL
            END,
            lifecycle_status = CASE
                WHEN candidates.source_rank = 1 THEN 'active'
                ELSE 'quarantined'
            END
        FROM candidates
        WHERE document.id = candidates.id
        """
    )
    op.execute(
        "UPDATE knowledge_documents SET "
        "active_revision_id = 'rev_legacy_' || substr(md5(id || text), 1, 20), "
        "desired_revision_id = 'rev_legacy_' || substr(md5(id || text), 1, 20), "
        "desired_sequence = 1"
    )
    op.create_check_constraint(
        "ck_knowledge_documents_lifecycle",
        "knowledge_documents",
        "lifecycle_status IN ('active', 'staging', 'superseded', "
        "'quarantined', 'deleting', 'deleted')",
    )
    op.create_index(
        "uq_knowledge_documents_collection_source",
        "knowledge_documents",
        ["collection_id", "source_id"],
        unique=True,
        postgresql_where=sa.text("source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_knowledge_documents_source_status",
        "knowledge_documents",
        ["source_id", "lifecycle_status"],
    )

    for column in (
        sa.Column("retrieval_context", sa.Text(), nullable=True),
        sa.Column("source_start", sa.BigInteger(), nullable=True),
        sa.Column("source_end", sa.BigInteger(), nullable=True),
        sa.Column("document_content_hash", sa.Text(), nullable=True),
        sa.Column("revision_id", sa.Text(), nullable=True),
        sa.Column("generation_id", sa.Text(), nullable=True),
    ):
        op.add_column("knowledge_chunks", column)
    op.execute(
        """
        UPDATE knowledge_chunks AS chunk
        SET revision_id = document.active_revision_id,
            generation_id = collection.active_generation_id
        FROM knowledge_documents AS document,
             knowledge_collections AS collection
        WHERE chunk.document_id = document.id
          AND document.collection_id = collection.id
        """
    )

    for column in (
        sa.Column(
            "phase", sa.Text(), nullable=False, server_default=sa.text("'queued'")
        ),
        sa.Column(
            "current_batch", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "total_batches", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "checkpoint",
            postgresql.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("generation_id", sa.Text(), nullable=True),
        sa.Column("fence_token", sa.Text(), nullable=True),
    ):
        op.add_column("indexing_jobs", column)
    op.drop_constraint("ck_indexing_jobs_status", "indexing_jobs", type_="check")
    op.create_check_constraint(
        "ck_indexing_jobs_status",
        "indexing_jobs",
        "status IN ('queued', 'running', 'cancelling', 'paused_dependency', "
        "'paused_validation', 'superseded', 'ready_raw_by_user_choice', "
        "'completed', 'failed', 'cancelled', 'expired')",
    )
    op.execute("DROP INDEX uq_indexing_jobs_active_collection")
    op.execute(
        "CREATE UNIQUE INDEX uq_indexing_jobs_active_collection "
        "ON indexing_jobs (collection_id) "
        "WHERE status IN ('queued', 'running', 'cancelling', "
        "'paused_dependency', 'paused_validation')"
    )


def downgrade() -> None:
    op.execute("DROP INDEX uq_indexing_jobs_active_collection")
    op.execute(
        "CREATE UNIQUE INDEX uq_indexing_jobs_active_collection "
        "ON indexing_jobs (collection_id) "
        "WHERE status IN ('queued', 'running', 'cancelling')"
    )
    op.drop_constraint("ck_indexing_jobs_status", "indexing_jobs", type_="check")
    op.create_check_constraint(
        "ck_indexing_jobs_status",
        "indexing_jobs",
        "status IN ('queued', 'running', 'cancelling', 'completed', "
        "'failed', 'cancelled', 'expired')",
    )
    for name in (
        "fence_token",
        "generation_id",
        "checkpoint",
        "total_batches",
        "current_batch",
        "phase",
    ):
        op.drop_column("indexing_jobs", name)
    for name in (
        "generation_id",
        "revision_id",
        "document_content_hash",
        "source_end",
        "source_start",
        "retrieval_context",
    ):
        op.drop_column("knowledge_chunks", name)
    op.drop_index(
        "ix_knowledge_documents_source_status", table_name="knowledge_documents"
    )
    op.drop_index(
        "uq_knowledge_documents_collection_source", table_name="knowledge_documents"
    )
    op.drop_constraint(
        "ck_knowledge_documents_lifecycle",
        "knowledge_documents",
        type_="check",
    )
    for name in (
        "lifecycle_status",
        "desired_sequence",
        "active_revision_id",
        "desired_revision_id",
        "source_id",
    ):
        op.drop_column("knowledge_documents", name)
    op.drop_column("knowledge_collections", "active_generation_id")
