"""Index llm_usage by run so per-run cost is not a sequential scan.

The ledger already indexes the user and model axes with the creation time.
"What did THIS run cost" is the question an operator asks first after a
surprising bill, and it was the one axis without an index.

Revision ID: 0074_llm_usage_run_index
Revises: 0073_llm_usage
"""

from __future__ import annotations

from alembic import op

revision = "0074_llm_usage_run_index"
down_revision = "0073_llm_usage"
branch_labels = None
depends_on = None

TABLE = "llm_usage"
INDEX = "ix_llm_usage_tenant_run"


def upgrade() -> None:
    op.create_index(INDEX, TABLE, ["tenant_id", "run_id"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
