"""Add the canonical source lifecycle and fencing authority.

Revision ID: 0058_source_lifecycle
Revises: 0057_asset_deletion

Source identity is scoped by tenant, owner, and workspace.  Upload/index writes
must hold the active epoch while aggregate deletion advances that epoch and
retains a minimal tombstone after all content-bearing records are removed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0058_source_lifecycle"
down_revision = "0057_asset_deletion"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    op.create_table(
        "source_lifecycles",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("owner_key", sa.Text(), primary_key=True),
        sa.Column("workspace_key", sa.Text(), primary_key=True),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("workspace_id", sa.Text(), nullable=True),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "epoch",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.CheckConstraint(
            "state IN ('active', 'deleting', 'deleted')",
            name="ck_source_lifecycles_state",
        ),
        sa.CheckConstraint(
            "epoch > 0",
            name="ck_source_lifecycles_epoch",
        ),
    )
    op.create_index(
        "ix_source_lifecycles_state",
        "source_lifecycles",
        ["tenant_id", "state", "updated_at"],
    )

    # Repair the historical ambiguity between the UI asset id (``fileId``)
    # and the blob-registry id (``file_id``).  A unique server-file binding is
    # safe to resolve; an ambiguous/missing binding is quarantined instead of
    # being guessed.  Every resolved row also receives canonical ``fileId``
    # metadata so exact deletion still matches quarantined duplicate rows.
    op.execute("DROP INDEX uq_knowledge_documents_collection_source")
    op.execute(
        """
        WITH server_file_counts AS (
            SELECT tenant_id, server_file_id, count(*) AS matches
            FROM asset_records
            WHERE server_file_id IS NOT NULL
            GROUP BY tenant_id, server_file_id
        ), resolved AS (
            SELECT
                document.id,
                document.collection_id,
                COALESCE(direct_asset.id, file_asset.id) AS asset_id,
                document.created_at
            FROM knowledge_documents AS document
            LEFT JOIN asset_records AS direct_asset
              ON direct_asset.tenant_id = document.tenant_id
             AND direct_asset.id = NULLIF(BTRIM(document.metadata->>'fileId'), '')
            LEFT JOIN server_file_counts AS file_count
              ON file_count.tenant_id = document.tenant_id
             AND file_count.server_file_id =
                 NULLIF(BTRIM(document.metadata->>'file_id'), '')
             AND file_count.matches = 1
            LEFT JOIN asset_records AS file_asset
              ON file_asset.tenant_id = document.tenant_id
             AND file_asset.server_file_id = file_count.server_file_id
            WHERE direct_asset.id IS NOT NULL OR file_asset.id IS NOT NULL
        ), ranked AS (
            SELECT
                id,
                asset_id,
                row_number() OVER (
                    PARTITION BY collection_id, asset_id
                    ORDER BY created_at DESC, id DESC
                ) AS source_rank
            FROM resolved
        )
        UPDATE knowledge_documents AS document
        SET metadata = jsonb_set(
                COALESCE(document.metadata::jsonb, '{}'::jsonb),
                '{fileId}',
                to_jsonb(ranked.asset_id),
                true
            )::json,
            source_id = CASE
                WHEN ranked.source_rank = 1 THEN 'asset:' || ranked.asset_id
                ELSE NULL
            END,
            lifecycle_status = CASE
                WHEN ranked.source_rank = 1 THEN document.lifecycle_status
                ELSE 'quarantined'
            END
        FROM ranked
        WHERE document.id = ranked.id
        """
    )
    op.execute(
        """
        WITH server_file_counts AS (
            SELECT tenant_id, server_file_id, count(*) AS matches
            FROM asset_records
            WHERE server_file_id IS NOT NULL
            GROUP BY tenant_id, server_file_id
        )
        UPDATE knowledge_documents AS document
        SET source_id = NULL,
            lifecycle_status = 'quarantined'
        WHERE document.source_id LIKE 'asset:%'
          AND NULLIF(BTRIM(document.metadata->>'fileId'), '') IS NULL
          AND NULLIF(BTRIM(document.metadata->>'file_id'), '') IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM server_file_counts AS file_count
              WHERE file_count.tenant_id = document.tenant_id
                AND file_count.server_file_id =
                    NULLIF(BTRIM(document.metadata->>'file_id'), '')
                AND file_count.matches = 1
          )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_knowledge_documents_collection_source "
        "ON knowledge_documents (collection_id, source_id) "
        "WHERE source_id IS NOT NULL"
    )

    # Asset ownership/workspace comes exclusively from the canonical asset
    # row.  Client-supplied Knowledge metadata is intentionally not used to
    # establish this security scope.  A failed deletion remains fenced.
    op.execute(
        """
        INSERT INTO source_lifecycles (
            tenant_id,
            source_id,
            owner_key,
            workspace_key,
            owner_user_id,
            workspace_id,
            state,
            epoch,
            operation_id,
            updated_at
        )
        SELECT
            asset.tenant_id,
            'asset:' || asset.id,
            COALESCE(asset.created_by_user_id::text, ''),
            COALESCE(asset.workspace_id, ''),
            asset.created_by_user_id,
            asset.workspace_id,
            CASE
                WHEN asset.lifecycle_status = 'active' THEN 'active'
                ELSE 'deleting'
            END,
            CASE
                WHEN asset.lifecycle_status = 'active' THEN 1
                ELSE 2
            END,
            asset.deletion_operation_id,
            asset.updated_at
        FROM asset_records AS asset
        """
    )

    # Sections are aggregate identities as well: deleting one must fence late
    # PUTs even after its content rows and time-bounded receipt are gone.
    op.execute(
        """
        INSERT INTO source_lifecycles (
            tenant_id,
            source_id,
            owner_key,
            workspace_key,
            owner_user_id,
            workspace_id,
            state,
            epoch,
            operation_id,
            updated_at
        )
        SELECT
            section.tenant_id,
            'section:' || section.id,
            COALESCE(section.created_by_user_id::text, ''),
            COALESCE(section.workspace_id, ''),
            section.created_by_user_id,
            section.workspace_id,
            CASE WHEN active_delete.operation_id IS NULL
                THEN 'active' ELSE 'deleting' END,
            CASE WHEN active_delete.operation_id IS NULL THEN 1 ELSE 2 END,
            active_delete.operation_id,
            GREATEST(
                section.updated_at,
                COALESCE(active_delete.updated_at, section.updated_at)
            )
        FROM asset_sections AS section
        LEFT JOIN LATERAL (
            SELECT operation.operation_id, operation.updated_at
            FROM deletion_operations AS operation
            WHERE operation.tenant_id = section.tenant_id
              AND operation.target_kind = 'section'
              AND operation.target_id = section.id
              AND operation.status IN ('queued', 'running', 'delete_failed')
            ORDER BY operation.created_at DESC, operation.operation_id DESC
            LIMIT 1
        ) AS active_delete ON true
        """
    )

    # Non-asset sources have no Asset aggregate.  Their durable scope is the
    # canonical collection owner; this keeps existing API-managed documents
    # writable without allowing their metadata to define an owner.
    op.execute(
        """
        INSERT INTO source_lifecycles (
            tenant_id,
            source_id,
            owner_key,
            workspace_key,
            owner_user_id,
            workspace_id,
            state,
            epoch,
            operation_id,
            updated_at
        )
        SELECT DISTINCT ON (
            collection.tenant_id,
            document.source_id,
            COALESCE(collection.created_by_user_id::text, '')
        )
            collection.tenant_id,
            document.source_id,
            COALESCE(collection.created_by_user_id::text, ''),
            '',
            collection.created_by_user_id,
            NULL,
            CASE
                WHEN document.lifecycle_status = 'deleted' THEN 'deleted'
                WHEN document.lifecycle_status = 'deleting' THEN 'deleting'
                ELSE 'active'
            END,
            CASE
                WHEN document.lifecycle_status IN ('deleting', 'deleted')
                    THEN 2
                ELSE 1
            END,
            NULL,
            GREATEST(document.created_at, collection.created_at)
        FROM knowledge_documents AS document
        JOIN knowledge_collections AS collection
          ON collection.id = document.collection_id
         AND collection.tenant_id = document.tenant_id
        WHERE document.source_id IS NOT NULL
          AND document.source_id NOT LIKE 'asset:%'
        ORDER BY
            collection.tenant_id,
            document.source_id,
            COALESCE(collection.created_by_user_id::text, ''),
            document.created_at DESC,
            document.id DESC
        """
    )

    op.execute(
        "REVOKE ALL PRIVILEGES ON TABLE source_lifecycles "
        "FROM PUBLIC, inqtrix_app"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE source_lifecycles "
        "TO inqtrix_app"
    )
    op.execute("ALTER TABLE source_lifecycles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE source_lifecycles FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON source_lifecycles
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "Source lifecycle fencing is irreversible: schema downgrade would "
        "discard deletion tombstones and allow late work to resurrect a "
        "deleted source. Restore the matching pre-upgrade database backup "
        "instead."
    )
