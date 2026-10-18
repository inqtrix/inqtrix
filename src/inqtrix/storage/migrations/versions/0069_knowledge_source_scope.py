"""Bind Knowledge documents to the canonical source authority scope.

Revision ID: 0069_knowledge_source_scope
Revises: 0068_release_integrity

``source_id`` is stable only inside its tenant/owner/workspace scope.  The
scope therefore lives in typed, server-owned columns rather than client
metadata.  Existing asset documents are reconciled from the canonical asset
row first and from one unambiguous retained lifecycle tombstone second.
Ambiguous active rows are quarantined instead of being assigned by guesswork.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0069_knowledge_source_scope"
down_revision = "0068_release_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "source_owner_user_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column("source_workspace_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "source_scope_bound",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # Canonical asset rows are the strongest source-scope authority.  The
    # fileId fallback covers retained quarantined duplicates that still need
    # to be removed with their aggregate.
    op.execute(
        """
        UPDATE knowledge_documents AS document
        SET source_owner_user_id = asset.created_by_user_id,
            source_workspace_id = asset.workspace_id,
            source_scope_bound = true
        FROM asset_records AS asset
        WHERE asset.tenant_id = document.tenant_id
          AND asset.id = COALESCE(
                CASE
                    WHEN document.source_id LIKE 'asset:%'
                        THEN NULLIF(substr(BTRIM(document.source_id), 7), '')
                    ELSE NULL
                END,
                CASE
                    WHEN BTRIM(document.metadata->>'fileId') LIKE 'asset:%'
                        THEN NULLIF(
                            substr(BTRIM(document.metadata->>'fileId'), 7),
                            ''
                        )
                    ELSE NULLIF(BTRIM(document.metadata->>'fileId'), '')
                END
          )
        """
    )

    # Deleted asset metadata is intentionally gone while its source lifecycle
    # tombstone remains.  Only a unique scope is safe to recover.
    op.execute(
        """
        WITH unique_lifecycle AS (
            SELECT
                tenant_id,
                source_id,
                min(owner_user_id::text)::uuid AS owner_user_id,
                min(workspace_id) AS workspace_id
            FROM source_lifecycles
            WHERE source_id LIKE 'asset:%'
            GROUP BY tenant_id, source_id
            HAVING count(*) = 1
        )
        UPDATE knowledge_documents AS document
        SET source_owner_user_id = lifecycle.owner_user_id,
            source_workspace_id = lifecycle.workspace_id,
            source_scope_bound = true
        FROM unique_lifecycle AS lifecycle
        WHERE lifecycle.tenant_id = document.tenant_id
          AND lifecycle.source_id = document.source_id
          AND document.source_id LIKE 'asset:%'
          AND document.source_owner_user_id IS NULL
          AND document.source_workspace_id IS NULL
        """
    )

    # API-managed sources have no Asset aggregate.  Their current canonical
    # scope is the owning collection, matching the source-lifecycle contract.
    op.execute(
        """
        UPDATE knowledge_documents AS document
        SET source_owner_user_id = collection.created_by_user_id,
            source_workspace_id = NULL,
            source_scope_bound = true
        FROM knowledge_collections AS collection
        WHERE collection.tenant_id = document.tenant_id
          AND collection.id = document.collection_id
          AND document.source_id IS NOT NULL
          AND document.source_id NOT LIKE 'asset:%'
        """
    )

    # An active asset row without a resolved scope cannot safely participate
    # in retrieval or aggregate deletion.  Preserve it for explicit repair.
    op.execute(
        """
        UPDATE knowledge_documents
        SET lifecycle_status = 'quarantined'
        WHERE source_id LIKE 'asset:%'
          AND lifecycle_status = 'active'
          AND NOT source_scope_bound
        """
    )

    op.create_index(
        "ix_knowledge_documents_source_scope_status",
        "knowledge_documents",
        [
            "tenant_id",
            "source_id",
            "source_owner_user_id",
            "source_workspace_id",
            "lifecycle_status",
        ],
    )


def downgrade() -> None:
    raise RuntimeError(
        "Knowledge source-scope binding is security-critical and cannot be "
        "removed in place. Restore the matching pre-upgrade backup instead."
    )
