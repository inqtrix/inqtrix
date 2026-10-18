"""Add acceptance state to resource shares.

Revision ID: 0028_shares_acceptance
Revises: 0027_user_bubble_tone

Resource shares gain an ``accepted_at`` timestamp so a recipient must consent
before a share grants access. ``NULL`` = pending (granted, awaiting consent);
non-NULL = accepted (active, grants access). The revoked columns are unchanged
(``revoked_at IS NOT NULL`` = inactive, whether revoked by the owner or
declined/left by the recipient).

Existing active rows are backfilled to ``created_at`` so shares minted before
this migration keep working without a re-consent (byte-identical access for
pre-existing grants); only newly minted shares start pending. The partial
unique index (``WHERE revoked_at IS NULL``) is intentionally untouched: there
is still exactly one active row per tuple, pending or accepted.
"""

from __future__ import annotations

from alembic import op

revision = "0028_shares_acceptance"
down_revision = "0027_user_bubble_tone"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE resource_shares "
        "ADD COLUMN IF NOT EXISTS accepted_at timestamptz NULL"
    )
    op.execute(
        "UPDATE resource_shares SET accepted_at = created_at "
        "WHERE accepted_at IS NULL AND revoked_at IS NULL"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE resource_shares DROP COLUMN IF EXISTS accepted_at")
