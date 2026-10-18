"""Separate account sessions from guest collaboration identities.

Revision ID: 0054_guest_lease_sessions
Revises: 0053_editor_guest_links

Account collaboration leases remain bound to ``auth_sessions``. Guest leases
instead use their document-scoped guest identity and therefore leave the
account-only session foreign key empty.
"""

import sqlalchemy as sa
from alembic import op

revision = "0054_guest_lease_sessions"
down_revision = "0053_editor_guest_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        type_="check",
    )
    op.alter_column(
        "editor_collaboration_leases",
        "session_id",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        "(actor_kind = 'user' AND user_id IS NOT NULL "
        "AND guest_identity_id IS NULL AND guest_link_id IS NULL "
        "AND session_id IS NOT NULL) OR "
        "(actor_kind = 'guest' AND user_id IS NULL "
        "AND guest_identity_id IS NOT NULL AND guest_link_id IS NOT NULL "
        "AND session_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        type_="check",
    )
    op.execute("DELETE FROM editor_collaboration_leases WHERE actor_kind = 'guest'")
    op.alter_column(
        "editor_collaboration_leases",
        "session_id",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_collaboration_leases_actor",
        "editor_collaboration_leases",
        "(actor_kind = 'user' AND user_id IS NOT NULL "
        "AND guest_identity_id IS NULL AND guest_link_id IS NULL) OR "
        "(actor_kind = 'guest' AND user_id IS NULL "
        "AND guest_identity_id IS NOT NULL AND guest_link_id IS NOT NULL)",
    )
