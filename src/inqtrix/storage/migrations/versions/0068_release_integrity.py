"""Reconcile source identity and enforce tenant-scoped ledger references.

Revision ID: 0068_release_integrity
Revises: 0067_session_deletion_contract

Client metadata is evidence for locating a canonical asset, never authority to
mint one. A document is attached only when every resolvable hint identifies
one asset in the same tenant and no second document in the collection claims
that asset. All other asset-like claims remain present but quarantined.
"""

from __future__ import annotations

from alembic import op

revision = "0068_release_integrity"
down_revision = "0067_session_deletion_contract"
branch_labels = None
depends_on = None


_SOURCE_RECONCILIATION_SQL = """
CREATE TEMP TABLE inqtrix_source_reconciliation ON COMMIT DROP AS
WITH claimed_documents AS (
    SELECT
        document.tenant_id,
        document.id AS document_id,
        document.collection_id,
        document.created_at,
        document.lifecycle_status,
        (CASE WHEN document.source_id LIKE 'asset:%' THEN 1 ELSE 0 END) +
        (CASE WHEN NULLIF(BTRIM(document.metadata->>'fileId'), '') IS NOT NULL
            THEN 1 ELSE 0 END) +
        (CASE WHEN NULLIF(BTRIM(document.metadata->>'file_id'), '') IS NOT NULL
            THEN 1 ELSE 0 END) AS hint_count
    FROM knowledge_documents AS document
    WHERE document.source_id LIKE 'asset:%'
       OR NULLIF(BTRIM(document.metadata->>'fileId'), '') IS NOT NULL
       OR NULLIF(BTRIM(document.metadata->>'file_id'), '') IS NOT NULL
), unique_server_files AS (
    SELECT tenant_id, server_file_id, min(id) AS asset_id
    FROM asset_records
    WHERE server_file_id IS NOT NULL
    GROUP BY tenant_id, server_file_id
    HAVING count(*) = 1
), candidates AS (
    SELECT document.tenant_id, document.id AS document_id,
           'source_id' AS hint_kind, asset.id AS asset_id
    FROM knowledge_documents AS document
    JOIN asset_records AS asset
      ON asset.tenant_id = document.tenant_id
     AND asset.id = NULLIF(substr(BTRIM(document.source_id), 7), '')
    WHERE document.source_id LIKE 'asset:%'
    UNION ALL
    SELECT document.tenant_id, document.id, 'fileId', asset.id
    FROM knowledge_documents AS document
    JOIN asset_records AS asset
      ON asset.tenant_id = document.tenant_id
     AND asset.id = CASE
            WHEN BTRIM(document.metadata->>'fileId') LIKE 'asset:%'
                THEN substr(BTRIM(document.metadata->>'fileId'), 7)
            ELSE NULLIF(BTRIM(document.metadata->>'fileId'), '')
        END
    WHERE NULLIF(BTRIM(document.metadata->>'fileId'), '') IS NOT NULL
    UNION ALL
    SELECT document.tenant_id, document.id, 'file_id', server_file.asset_id
    FROM knowledge_documents AS document
    JOIN unique_server_files AS server_file
      ON server_file.tenant_id = document.tenant_id
     AND server_file.server_file_id =
         NULLIF(BTRIM(document.metadata->>'file_id'), '')
), per_document AS (
    SELECT
        claimed.tenant_id,
        claimed.document_id,
        claimed.collection_id,
        claimed.created_at,
        claimed.lifecycle_status,
        claimed.hint_count,
        count(candidate.asset_id) AS resolved_hint_count,
        count(DISTINCT candidate.asset_id) AS candidate_count,
        min(candidate.asset_id) AS asset_id
    FROM claimed_documents AS claimed
    LEFT JOIN candidates AS candidate
      ON candidate.tenant_id = claimed.tenant_id
     AND candidate.document_id = claimed.document_id
    GROUP BY
        claimed.tenant_id,
        claimed.document_id,
        claimed.collection_id,
        claimed.created_at,
        claimed.lifecycle_status,
        claimed.hint_count
), classified AS (
    SELECT
        per_document.*,
        count(*) FILTER (
            WHERE candidate_count = 1
              AND resolved_hint_count = hint_count
        ) OVER (
            PARTITION BY tenant_id, collection_id, asset_id
        ) AS collection_asset_claims
    FROM per_document
)
SELECT * FROM classified
"""


_TENANT_REFERENCE_PREFLIGHT_SQL = """
DO $$
DECLARE mismatch_count bigint;
BEGIN
    SELECT count(*) INTO mismatch_count FROM (
        SELECT 1
        FROM deletion_operation_assets AS child
        JOIN deletion_operations AS parent
          ON parent.operation_id = child.operation_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM deletion_operation_events AS child
        JOIN deletion_operations AS parent
          ON parent.operation_id = child.operation_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM upload_operations AS child
        JOIN asset_records AS parent ON parent.id = child.asset_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM upload_operation_events AS child
        JOIN upload_operations AS parent
          ON parent.operation_id = child.operation_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM upload_operation_outbox AS child
        JOIN upload_operations AS parent
          ON parent.operation_id = child.operation_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM knowledge_document_revisions AS child
        JOIN knowledge_documents AS parent ON parent.id = child.document_id
        WHERE child.tenant_id <> parent.tenant_id
        UNION ALL
        SELECT 1
        FROM knowledge_index_generations AS child
        JOIN knowledge_collections AS parent ON parent.id = child.collection_id
        WHERE child.tenant_id <> parent.tenant_id
    ) AS mismatches;
    IF mismatch_count <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Tenant reference migration blocked: ' ||
                      mismatch_count ||
                      ' cross-tenant ledger relationship(s) require ' ||
                      'explicit remediation.';
    END IF;
END
$$
"""


_TENANT_CONSTRAINTS = (
    "ALTER TABLE deletion_operations ADD CONSTRAINT "
    "uq_deletion_operations_tenant_operation UNIQUE (tenant_id, operation_id)",
    "ALTER TABLE upload_operations ADD CONSTRAINT "
    "uq_upload_operations_tenant_operation UNIQUE (tenant_id, operation_id)",
    "ALTER TABLE asset_records ADD CONSTRAINT "
    "uq_asset_records_tenant_id UNIQUE (tenant_id, id)",
    "ALTER TABLE knowledge_documents ADD CONSTRAINT "
    "uq_knowledge_documents_tenant_id UNIQUE (tenant_id, id)",
    "ALTER TABLE knowledge_collections ADD CONSTRAINT "
    "uq_knowledge_collections_tenant_id UNIQUE (tenant_id, id)",
    "ALTER TABLE deletion_operation_assets ADD CONSTRAINT "
    "fk_deletion_operation_assets_tenant_operation FOREIGN KEY "
    "(tenant_id, operation_id) REFERENCES deletion_operations "
    "(tenant_id, operation_id) ON DELETE CASCADE",
    "ALTER TABLE deletion_operation_events ADD CONSTRAINT "
    "fk_deletion_operation_events_tenant_operation FOREIGN KEY "
    "(tenant_id, operation_id) REFERENCES deletion_operations "
    "(tenant_id, operation_id) ON DELETE CASCADE",
    "ALTER TABLE upload_operations ADD CONSTRAINT "
    "fk_upload_operations_tenant_asset FOREIGN KEY (tenant_id, asset_id) "
    "REFERENCES asset_records (tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE upload_operation_events ADD CONSTRAINT "
    "fk_upload_operation_events_tenant_operation FOREIGN KEY "
    "(tenant_id, operation_id) REFERENCES upload_operations "
    "(tenant_id, operation_id) ON DELETE CASCADE",
    "ALTER TABLE upload_operation_outbox ADD CONSTRAINT "
    "fk_upload_operation_outbox_tenant_operation FOREIGN KEY "
    "(tenant_id, operation_id) REFERENCES upload_operations "
    "(tenant_id, operation_id) ON DELETE CASCADE",
    "ALTER TABLE knowledge_document_revisions ADD CONSTRAINT "
    "fk_knowledge_revisions_tenant_document FOREIGN KEY "
    "(tenant_id, document_id) REFERENCES knowledge_documents "
    "(tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE knowledge_index_generations ADD CONSTRAINT "
    "fk_knowledge_generations_tenant_collection FOREIGN KEY "
    "(tenant_id, collection_id) REFERENCES knowledge_collections "
    "(tenant_id, id) ON DELETE CASCADE",
)


_QUOTA_POSTCONDITION_SQL = """
DO $$
DECLARE counter_mismatches bigint;
DECLARE stock_mismatches bigint;
BEGIN
    WITH expected AS (
        SELECT tenant_id, owner_user_id AS subject_user_id,
               SUM(GREATEST(0, size_bytes)) AS used
        FROM files
        WHERE owner_user_id IS NOT NULL
        GROUP BY tenant_id, owner_user_id
    ), actual AS (
        SELECT tenant_id, subject_user_id, used
        FROM quota_usage_counters
        WHERE dimension = 'stored_bytes' AND period_start = 0
    )
    SELECT count(*) INTO counter_mismatches
    FROM expected FULL OUTER JOIN actual
      USING (tenant_id, subject_user_id)
    WHERE COALESCE(expected.used, 0) <> COALESCE(actual.used, 0);

    WITH expected AS (
        SELECT DISTINCT original.tenant_id,
               'file:' || original.id AS stock_key,
               original.owner_user_id AS subject_user_id,
               GREATEST(0, original.size_bytes) AS amount
        FROM files AS original
        JOIN asset_records AS asset
          ON asset.tenant_id = original.tenant_id
         AND asset.server_file_id = original.id
        WHERE original.owner_user_id IS NOT NULL
    ), actual AS (
        SELECT tenant_id, stock_key, subject_user_id, amount
        FROM quota_stock_lifecycles
        WHERE dimension = 'stored_bytes' AND NOT tombstoned
    )
    SELECT count(*) INTO stock_mismatches
    FROM expected FULL OUTER JOIN actual USING (tenant_id, stock_key)
    WHERE expected.stock_key IS NULL
       OR actual.stock_key IS NULL
       OR expected.subject_user_id <> actual.subject_user_id
       OR expected.amount <> actual.amount;

    IF counter_mismatches <> 0 OR stock_mismatches <> 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'Quota lifecycle postcondition failed: counters=' ||
                      counter_mismatches || ', stock=' || stock_mismatches ||
                      '. Keep workloads quiesced and reconcile the exact rows.';
    END IF;
END
$$
"""


def upgrade() -> None:
    op.execute(_SOURCE_RECONCILIATION_SQL)
    op.execute(
        """
        UPDATE knowledge_documents AS document
        SET source_id = NULL,
            lifecycle_status = CASE
                WHEN document.lifecycle_status IN ('active', 'quarantined')
                    THEN 'quarantined'
                ELSE document.lifecycle_status
            END
        FROM inqtrix_source_reconciliation AS reconciliation
        WHERE reconciliation.tenant_id = document.tenant_id
          AND reconciliation.document_id = document.id
        """
    )
    op.execute(
        """
        UPDATE knowledge_documents AS document
        SET source_id = 'asset:' || reconciliation.asset_id,
            metadata = jsonb_set(
                COALESCE(document.metadata::jsonb, '{}'::jsonb),
                '{fileId}',
                to_jsonb(reconciliation.asset_id),
                true
            )::json,
            lifecycle_status = CASE
                WHEN reconciliation.lifecycle_status IN ('active', 'quarantined')
                    THEN 'active'
                ELSE reconciliation.lifecycle_status
            END
        FROM inqtrix_source_reconciliation AS reconciliation
        WHERE reconciliation.tenant_id = document.tenant_id
          AND reconciliation.document_id = document.id
          AND reconciliation.candidate_count = 1
          AND reconciliation.resolved_hint_count = reconciliation.hint_count
          AND reconciliation.collection_asset_claims = 1
        """
    )
    op.execute(
        """
        INSERT INTO source_lifecycles (
            tenant_id, source_id, owner_key, workspace_key,
            owner_user_id, workspace_id, state, epoch, operation_id, updated_at
        )
        SELECT
            asset.tenant_id,
            'asset:' || asset.id,
            COALESCE(asset.created_by_user_id::text, ''),
            COALESCE(asset.workspace_id, ''),
            asset.created_by_user_id,
            asset.workspace_id,
            CASE WHEN asset.lifecycle_status = 'active'
                THEN 'active' ELSE 'deleting' END,
            CASE WHEN asset.lifecycle_status = 'active' THEN 1 ELSE 2 END,
            asset.deletion_operation_id,
            asset.updated_at
        FROM asset_records AS asset
        ON CONFLICT (tenant_id, source_id, owner_key, workspace_key) DO NOTHING
        """
    )
    op.execute(
        """
        DO $$
        DECLARE invalid_active bigint;
        BEGIN
            SELECT count(*) INTO invalid_active
            FROM knowledge_documents AS document
            LEFT JOIN asset_records AS asset
              ON asset.tenant_id = document.tenant_id
             AND 'asset:' || asset.id = document.source_id
            WHERE document.lifecycle_status = 'active'
              AND document.source_id LIKE 'asset:%'
              AND (
                  asset.id IS NULL OR
                  (SELECT count(*) FROM source_lifecycles AS lifecycle
                   WHERE lifecycle.tenant_id = document.tenant_id
                     AND lifecycle.source_id = document.source_id) <> 1
              );
            IF invalid_active <> 0 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'Source reconciliation failed: ' || invalid_active ||
                              ' active asset source(s) lack one canonical ' ||
                              'asset lifecycle.';
            END IF;
        END
        $$
        """
    )

    op.execute(_TENANT_REFERENCE_PREFLIGHT_SQL)
    for table, constraint in (
        ("deletion_operation_assets", "deletion_operation_assets_operation_id_fkey"),
        ("deletion_operation_events", "deletion_operation_events_operation_id_fkey"),
        ("upload_operations", "upload_operations_asset_id_fkey"),
        ("upload_operation_events", "upload_operation_events_operation_id_fkey"),
        ("upload_operation_outbox", "upload_operation_outbox_operation_id_fkey"),
        (
            "knowledge_document_revisions",
            "knowledge_document_revisions_document_id_fkey",
        ),
        (
            "knowledge_index_generations",
            "knowledge_index_generations_collection_id_fkey",
        ),
    ):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}")
    for statement in _TENANT_CONSTRAINTS:
        op.execute(statement)

    op.execute(_QUOTA_POSTCONDITION_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "Release integrity reconciliation is irreversible: schema downgrade "
        "would remove tenant-scoped references and cannot reconstruct "
        "quarantined client identity claims. Restore the matching pre-upgrade "
        "database backup instead."
    )
