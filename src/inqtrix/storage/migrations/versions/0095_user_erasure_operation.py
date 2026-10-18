"""Erase an account as a durable deletion operation.

Revision ID: 0095_user_erasure_operation
Revises: 0094_incognito_runs

An account is no longer erased in one long transaction under the tenant's
security lock. The administrator's request starts a deletion operation of
the new target kind ``user``; the deletion worker removes the account's
aggregates through the deletion machine, its rows in short batches, and
the ``users`` row last.

* ``users.erasure_operation_id`` marks an account whose erasure has
  started. While it is set, no sign-in admits the account and it cannot be
  enabled again; the deletion machine acts as its owner.
* ``deletion_operations`` accepts the target kind ``user`` and the two
  checkpoints of that operation, ``aggregates_removed`` and
  ``rows_removed``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0095_user_erasure_operation"
down_revision = "0094_incognito_runs"
branch_labels = None
depends_on = None

_KINDS_BEFORE = (
    "'asset', 'bulk', 'group', 'section', 'vector_index', "
    "'knowledge_collection', 'knowledge_document', 'agent_session', "
    "'knowledge_session'"
)
_STAGES_BEFORE = (
    "'queued', 'vector_index_detached', 'indexing_cancelled', "
    "'search_detached', 'vectors_removed', 'knowledge_removed', "
    "'session_data_removed', 'blobs_removed', 'metadata_removed', "
    "'residuals_verified', 'delete_failed', 'deleted'"
)


def _replace_checks(*, kinds: str, stages: str) -> None:
    op.drop_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_target_kind",
        "deletion_operations",
        f"target_kind IN ({kinds})",
    )
    op.drop_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        type_="check",
    )
    op.create_check_constraint(
        "ck_deletion_operations_stage",
        "deletion_operations",
        f"stage IN ({stages})",
    )


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("erasure_operation_id", sa.Text(), nullable=True),
    )
    _replace_checks(
        kinds=f"{_KINDS_BEFORE}, 'user'",
        stages=f"{_STAGES_BEFORE}, 'aggregates_removed', 'rows_removed'",
    )


def downgrade() -> None:
    raise RuntimeError(
        "Durable account erasure is irreversible: schema downgrade would "
        "discard erasure receipts and could expose a partially erased "
        "account as enabled. Restore the matching pre-upgrade database "
        "backup instead."
    )
