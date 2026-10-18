"""Widen the vector_index_members state CHECK to allow 'skipped'.

Revision ID: 0025_vector_index_member_skipped
Revises: 0024_vector_index_member_doc_id

A no-extractable-text document is now a TERMINAL ``skipped`` member state (it can
never embed), distinct from ``pending`` (queued, will embed) so the UI stops
prompting a futile re-index and the index reads ``ready`` once nothing is
genuinely pending. The frontend union and the service validator
(``_VALID_MEMBER_STATE``) already enumerate it; this widens the durable tier's
CHECK constraint ``ck_vector_index_members_state`` (added in
``0016_vector_index_records``) so a ``skipped`` member actually persists instead
of raising an IntegrityError on insert.

Idempotent: ``DROP CONSTRAINT IF EXISTS`` then re-add covers both an
already-migrated database (the 0016 constraint exists) and a freshly created one
(``create_all`` builds the table without the raw-SQL CHECK, so the DROP is a
no-op and the ADD installs the widened form).
"""

from __future__ import annotations

from alembic import op

revision = "0025_vector_index_member_skipped"
down_revision = "0024_vector_index_member_doc_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_members "
        "DROP CONSTRAINT IF EXISTS ck_vector_index_members_state"
    )
    op.execute(
        "ALTER TABLE vector_index_members "
        "ADD CONSTRAINT ck_vector_index_members_state "
        "CHECK (state IN ('pending', 'embedded', 'skipped'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE vector_index_members "
        "DROP CONSTRAINT IF EXISTS ck_vector_index_members_state"
    )
    op.execute(
        "ALTER TABLE vector_index_members "
        "ADD CONSTRAINT ck_vector_index_members_state "
        "CHECK (state IN ('pending', 'embedded'))"
    )
