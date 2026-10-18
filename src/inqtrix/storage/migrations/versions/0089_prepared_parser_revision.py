"""Record which parser revision produced an asset's canonical text.

Revision ID: 0089_prepared_parser_revision
Revises: 0088_user_erasure

The canonical prepared text is identified by the file digest, the text
digest and the parser id. The parser id names the implementation, not its
revision: a later library release can extract the same file differently, and
a reader that pins a version needs to tell the two apart. New preparations
record the revision they were made with. Existing rows keep NULL, which
readers show as "unknown"; the revision installed today says nothing about
what produced a text months ago, so it is never back-filled.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089_prepared_parser_revision"
down_revision = "0088_user_erasure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "asset_records",
        sa.Column("prepared_parser_revision", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_asset_records_prepared_parser_revision",
        "asset_records",
        "prepared_parser_revision IS NULL OR prepared_text IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_asset_records_prepared_parser_revision", "asset_records", type_="check"
    )
    op.drop_column("asset_records", "prepared_parser_revision")
