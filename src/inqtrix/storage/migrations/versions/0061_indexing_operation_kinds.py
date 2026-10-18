"""Run document revisions on the durable indexing worker infrastructure.

Revision ID: 0061_indexing_operation_kinds
Revises: 0060_knowledge_history

The existing job/event/queue lifecycle remains the single execution plane.
Operation identity distinguishes collection-generation builds from immutable
document revisions; only generation builds reserve the collection-wide slot.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061_indexing_operation_kinds"
down_revision = "0060_knowledge_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "indexing_jobs",
        sa.Column(
            "operation_kind",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'collection_generation'"),
        ),
    )
    op.add_column(
        "indexing_jobs", sa.Column("document_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "indexing_jobs", sa.Column("revision_id", sa.Text(), nullable=True)
    )
    op.execute(
        """
        UPDATE indexing_jobs
        SET generation_id = 'gen_legacy_' || substr(md5(job_id), 1, 20)
        WHERE generation_id IS NULL
        """
    )
    op.create_check_constraint(
        "ck_indexing_jobs_operation_kind",
        "indexing_jobs",
        "operation_kind IN ('collection_generation', 'document_revision')",
    )
    op.create_check_constraint(
        "ck_indexing_jobs_operation_identity",
        "indexing_jobs",
        "(operation_kind = 'collection_generation' AND generation_id IS NOT NULL "
        "AND document_id IS NULL AND revision_id IS NULL) OR "
        "(operation_kind = 'document_revision' AND generation_id IS NULL "
        "AND document_id IS NOT NULL AND revision_id IS NOT NULL)",
    )
    op.drop_index(
        "uq_indexing_jobs_active_collection", table_name="indexing_jobs"
    )
    op.create_index(
        "uq_indexing_jobs_active_collection",
        "indexing_jobs",
        ["collection_id"],
        unique=True,
        postgresql_where=sa.text(
            "operation_kind = 'collection_generation' AND "
            "status IN ('queued', 'running', 'cancelling', "
            "'paused_dependency', 'paused_validation')"
        ),
    )
    op.create_index(
        "ix_indexing_jobs_revision_created",
        "indexing_jobs",
        ["revision_id", "created_at"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM indexing_jobs
                WHERE operation_kind = 'document_revision'
            ) THEN
                RAISE EXCEPTION
                    'Indexing-operation downgrade blocked: document revision '
                    'jobs exist. Finish or remove those rows explicitly.';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_indexing_jobs_revision_created", table_name="indexing_jobs"
    )
    op.drop_index(
        "uq_indexing_jobs_active_collection", table_name="indexing_jobs"
    )
    op.create_index(
        "uq_indexing_jobs_active_collection",
        "indexing_jobs",
        ["collection_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('queued', 'running', 'cancelling', "
            "'paused_dependency', 'paused_validation')"
        ),
    )
    op.drop_constraint(
        "ck_indexing_jobs_operation_identity",
        "indexing_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_indexing_jobs_operation_kind",
        "indexing_jobs",
        type_="check",
    )
    op.drop_column("indexing_jobs", "revision_id")
    op.drop_column("indexing_jobs", "document_id")
    op.drop_column("indexing_jobs", "operation_kind")
