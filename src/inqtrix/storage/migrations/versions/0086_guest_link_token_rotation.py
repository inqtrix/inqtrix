"""Admit token rotation as its own guest-link command kind.

Rotating the link half is a distinct audited command, not an `update`: it
replaces the stored token digest, ends every session opened with the old
pair, and must stay distinguishable in the idempotent command record. The
existing check constraint whitelisted the four kinds that predate it, so the
new one has to be added before the service can write it.
"""

from alembic import op

revision = "0086_guest_link_token_rotation"
down_revision = "0085_session_title_revision"
branch_labels = None
depends_on = None

_TABLE = "editor_document_share_links"
_NAME = "ck_editor_document_share_links_command_kind"
_BEFORE = "last_command_kind IN ('create', 'update', 'revoke', 'rotate_password')"
_AFTER = (
    "last_command_kind IN "
    "('create', 'update', 'revoke', 'rotate_password', 'rotate_token')"
)


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _AFTER)


def downgrade() -> None:
    # Rows written by the new command kind would violate the narrower rule,
    # so they are folded onto the closest surviving kind first.
    op.execute(
        f"UPDATE {_TABLE} SET last_command_kind = 'update' "
        "WHERE last_command_kind = 'rotate_token'"
    )
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _BEFORE)
