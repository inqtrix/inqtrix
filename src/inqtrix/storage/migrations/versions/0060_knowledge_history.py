"""Add immutable knowledge revisions and rollbackable index generations.

Revision ID: 0060_knowledge_history
Revises: 0059_durable_upload

The mutable document row remains the compatibility projection used by current
HTTP contracts.  Immutable revision rows own source/build identity, while a
generation ledger records every physical collection build and its bounded
rollback window.  Legacy evidence is backfilled only when its exact occurrence
is unambiguous; everything else remains unverified until rebuilt.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0060_knowledge_history"
down_revision = "0059_durable_upload"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"
_TABLES = (
    "knowledge_document_revisions",
    "knowledge_index_generations",
)


_CANONICAL_CONTENT_HASH_SQL = (
    "encode(sha256(convert_to(document.text, 'UTF8')), 'hex')"
)


def _backfill_verified_legacy_hashes() -> None:
    """Attach canonical hashes with two set-based PostgreSQL statements.

    PostgreSQL 15 provides ``sha256(bytea)`` without an extension. Keeping the
    digest inside the database avoids materializing every document body in the
    migration process and avoids two client round trips per document. Chunks
    without an exact recovered span deliberately retain a null hash until a
    shadow rebuild replaces them.
    """

    op.execute(
        f"""
        UPDATE knowledge_document_revisions AS revision
        SET content_hash = {_CANONICAL_CONTENT_HASH_SQL}
        FROM knowledge_documents AS document
        WHERE revision.tenant_id = document.tenant_id
          AND revision.document_id = document.id
          AND revision.revision_id = document.active_revision_id
          AND document.active_revision_id IS NOT NULL
        """
    )
    op.execute(
        f"""
        UPDATE knowledge_chunks AS chunk
        SET document_content_hash = {_CANONICAL_CONTENT_HASH_SQL}
        FROM knowledge_documents AS document
        WHERE chunk.tenant_id = document.tenant_id
          AND chunk.document_id = document.id
          AND chunk.revision_id = document.active_revision_id
          AND chunk.source_start IS NOT NULL
          AND chunk.source_end IS NOT NULL
          AND chunk.source_text <> ''
        """
    )


def upgrade() -> None:
    op.create_table(
        "knowledge_document_revisions",
        sa.Column("revision_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "document_id",
            sa.Text(),
            sa.ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collection_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("build_contract_hash", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'staging'"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=True),
        sa.Column("superseded_at", sa.Float(), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "collection_id",
            "source_id",
            "content_hash",
            "build_contract_hash",
            name="uq_knowledge_revision_build_identity",
        ),
        sa.CheckConstraint(
            "status IN ('staging', 'active', 'superseded', 'failed', 'deleted')",
            name="ck_knowledge_document_revisions_status",
        ),
    )
    op.create_index(
        "ix_knowledge_revisions_document_created",
        "knowledge_document_revisions",
        ["tenant_id", "document_id", "created_at"],
    )

    op.create_table(
        "knowledge_index_generations",
        sa.Column("generation_id", sa.Text(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
        sa.Column(
            "collection_id",
            sa.Text(),
            sa.ForeignKey("knowledge_collections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("build_contract_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'building'"),
        ),
        sa.Column(
            "manifest",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "validation",
            postgresql.JSON(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("activated_at", sa.Float(), nullable=True),
        sa.Column("superseded_at", sa.Float(), nullable=True),
        sa.Column("rollback_until", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "status IN ('building', 'active', 'rollback_available', 'failed', "
            "'deleted')",
            name="ck_knowledge_index_generations_status",
        ),
    )
    op.create_index(
        "ix_knowledge_generations_collection_status",
        "knowledge_index_generations",
        ["tenant_id", "collection_id", "status", "created_at"],
    )

    # Keep every currently visible document as one immutable compatibility
    # revision. Existing coherent hashes are retained here; after exact span
    # recovery below, the set-based PostgreSQL backfill derives the canonical
    # hash without materializing document bodies in the migration process.
    op.execute(
        """
        INSERT INTO knowledge_document_revisions (
            revision_id, tenant_id, document_id, collection_id, source_id,
            content_hash, build_contract_hash, title, text, metadata, status,
            created_at, activated_at, superseded_at
        )
        SELECT
            COALESCE(
                document.active_revision_id,
                document.desired_revision_id,
                'rev_legacy_' || substr(md5(document.id || document.text), 1, 20)
            ),
            document.tenant_id,
            document.id,
            document.collection_id,
            document.source_id,
            COALESCE(chunk_hash.content_hash,
                'legacy-unverified:' || md5(document.id || document.text)),
            'legacy-unverified-build',
            document.title,
            document.text,
            document.metadata,
            CASE WHEN document.lifecycle_status = 'active'
                THEN 'active' ELSE 'superseded' END,
            document.created_at,
            CASE WHEN document.lifecycle_status = 'active'
                THEN document.created_at ELSE NULL END,
            CASE WHEN document.lifecycle_status = 'active'
                THEN NULL ELSE document.created_at END
        FROM knowledge_documents AS document
        LEFT JOIN LATERAL (
            SELECT min(chunk.document_content_hash) AS content_hash
            FROM knowledge_chunks AS chunk
            WHERE chunk.tenant_id = document.tenant_id
              AND chunk.document_id = document.id
              AND chunk.revision_id = document.active_revision_id
              AND chunk.document_content_hash IS NOT NULL
            HAVING count(DISTINCT chunk.document_content_hash) = 1
        ) AS chunk_hash ON true
        """
    )
    op.execute(
        """
        UPDATE knowledge_documents AS document
        SET active_revision_id = revision.revision_id,
            desired_revision_id = COALESCE(
                document.desired_revision_id, revision.revision_id
            ),
            desired_sequence = GREATEST(document.desired_sequence, 1)
        FROM knowledge_document_revisions AS revision
        WHERE revision.document_id = document.id
          AND revision.tenant_id = document.tenant_id
          AND document.active_revision_id IS NULL
          AND document.lifecycle_status = 'active'
        """
    )

    op.execute(
        """
        INSERT INTO knowledge_index_generations (
            generation_id, tenant_id, collection_id, build_contract_hash,
            status, manifest, validation, created_at, activated_at,
            superseded_at, rollback_until
        )
        SELECT
            collection.active_generation_id,
            collection.tenant_id,
            collection.id,
            'legacy-unverified-build',
            'active',
            COALESCE(manifest.value, '{}'::json),
            json_build_object('backfill', 'legacy'),
            collection.created_at,
            collection.created_at,
            NULL,
            NULL
        FROM knowledge_collections AS collection
        LEFT JOIN LATERAL (
            SELECT json_object_agg(document.id, document.active_revision_id) AS value
            FROM knowledge_documents AS document
            WHERE document.tenant_id = collection.tenant_id
              AND document.collection_id = collection.id
              AND document.lifecycle_status = 'active'
        ) AS manifest ON true
        WHERE collection.active_generation_id IS NOT NULL
        """
    )

    # Only an exact, unique source occurrence receives a recovered span.
    # Missing, combined, or repeated legacy text stays unspanned and therefore
    # cannot become reader-facing evidence until a rebuild.
    op.execute(
        """
        UPDATE knowledge_chunks AS chunk
        SET source_start = octet_length(
                left(document.text, strpos(document.text, chunk.source_text) - 1)
            ),
            source_end = octet_length(
                left(
                    document.text,
                    strpos(document.text, chunk.source_text) - 1
                    + char_length(chunk.source_text)
                )
            )
        FROM knowledge_documents AS document
        WHERE chunk.tenant_id = document.tenant_id
          AND chunk.document_id = document.id
          AND chunk.source_start IS NULL
          AND chunk.source_end IS NULL
          AND chunk.source_text <> ''
          AND strpos(document.text, chunk.source_text) > 0
          AND strpos(document.text, chunk.source_text) =
              char_length(document.text)
              - strpos(reverse(document.text), reverse(chunk.source_text))
              - char_length(chunk.source_text) + 2
        """
    )
    _backfill_verified_legacy_hashes()

    for table in _TABLES:
        op.execute(
            f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC, {APP_ROLE}"
        )
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO {APP_ROLE}"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    raise RuntimeError(
        "Knowledge history is irreversible: schema downgrade would discard "
        "immutable document revisions and index-generation rollback state. "
        "Restore the matching pre-upgrade database backup instead."
    )
