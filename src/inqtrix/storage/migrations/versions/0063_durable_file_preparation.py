"""Persist canonical server parsing inside the durable upload lifecycle.

Revision ID: 0063_durable_file_preparation
Revises: 0062_vector_index_deletion

The original-file operation now hands parsing to the worker after bytes and
asset binding are durable.  Canonical prepared text is stored separately from
the client-editable asset body and remains bound to the exact file digest.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0063_durable_file_preparation"
down_revision = "0062_vector_index_deletion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset_records", sa.Column("prepared_text", sa.Text(), nullable=True))
    op.add_column(
        "asset_records", sa.Column("prepared_parser_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "asset_records", sa.Column("prepared_content_hash", sa.Text(), nullable=True)
    )
    op.add_column(
        "asset_records", sa.Column("prepared_file_sha256", sa.Text(), nullable=True)
    )
    op.add_column(
        "asset_records", sa.Column("prepared_page_texts", sa.JSON(), nullable=True)
    )
    op.add_column(
        "asset_records", sa.Column("prepared_at", sa.Float(), nullable=True)
    )
    op.create_check_constraint(
        "ck_asset_records_prepared_text_identity",
        "asset_records",
        "(prepared_text IS NULL AND prepared_parser_id IS NULL "
        "AND prepared_content_hash IS NULL AND prepared_file_sha256 IS NULL "
        "AND prepared_page_texts IS NULL AND prepared_at IS NULL) OR "
        "(prepared_text IS NOT NULL AND length(prepared_text) > 0 "
        "AND prepared_parser_id IS NOT NULL AND prepared_content_hash IS NOT NULL "
        "AND prepared_file_sha256 IS NOT NULL "
        "AND prepared_page_texts IS NOT NULL AND prepared_at IS NOT NULL)",
    )

    op.drop_constraint("ck_asset_records_upload_status", "asset_records", type_="check")
    op.create_check_constraint(
        "ck_asset_records_upload_status",
        "asset_records",
        "upload_status IN ('awaiting_upload', 'uploading', 'retrying', "
        "'parsing', 'finalizing', 'ready', 'failed', 'cancelled')",
    )
    op.drop_constraint("ck_upload_operations_stage", "upload_operations", type_="check")
    op.create_check_constraint(
        "ck_upload_operations_stage",
        "upload_operations",
        "stage IN ('prepared', 'object_stored', 'file_registered', "
        "'asset_bound', 'parsing', 'parse_finished', 'quota_booked', 'ready')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    prepared = connection.execute(
        sa.text(
            "SELECT count(*) FROM asset_records "
            "WHERE prepared_text IS NOT NULL OR prepared_parser_id IS NOT NULL "
            "OR prepared_content_hash IS NOT NULL OR prepared_file_sha256 IS NOT NULL "
            "OR prepared_page_texts IS NOT NULL OR prepared_at IS NOT NULL"
        )
    ).scalar_one()
    parsing = connection.execute(
        sa.text(
            "SELECT count(*) FROM upload_operations "
            "WHERE stage IN ('parsing', 'parse_finished') OR status <> 'ready'"
        )
    ).scalar_one()
    if prepared or parsing:
        raise RuntimeError(
            "Durable file-preparation downgrade blocked: canonical prepared "
            f"assets={prepared}, non-downgradable upload operations={parsing}. "
            "Export or explicitly remediate those records before retrying."
        )

    op.drop_constraint("ck_upload_operations_stage", "upload_operations", type_="check")
    op.create_check_constraint(
        "ck_upload_operations_stage",
        "upload_operations",
        "stage IN ('prepared', 'object_stored', 'file_registered', "
        "'asset_bound', 'quota_booked', 'ready')",
    )
    op.drop_constraint("ck_asset_records_upload_status", "asset_records", type_="check")
    op.create_check_constraint(
        "ck_asset_records_upload_status",
        "asset_records",
        "upload_status IN ('awaiting_upload', 'uploading', 'retrying', "
        "'finalizing', 'ready', 'failed', 'cancelled')",
    )
    op.drop_constraint(
        "ck_asset_records_prepared_text_identity", "asset_records", type_="check"
    )
    op.drop_column("asset_records", "prepared_at")
    op.drop_column("asset_records", "prepared_page_texts")
    op.drop_column("asset_records", "prepared_file_sha256")
    op.drop_column("asset_records", "prepared_content_hash")
    op.drop_column("asset_records", "prepared_parser_id")
    op.drop_column("asset_records", "prepared_text")
