"""Instance role on the users mirror (admin | user).

Revision ID: 0009_instance_role
Revises: 0008_local_credentials

Adds the instance-wide role column that the admin surface gates on
(distinct from the per-workspace ``WorkspaceRole``). Additive and
non-rewriting: a NOT NULL column with a ``'user'`` server default, so
every existing mirror row becomes a regular user. No new grants/policy —
``users`` already carries the ``inqtrix_app`` DML grant and tenant RLS.

Idempotent on the column, by necessity: unlike every other migration
(each ``create_all``-s its own metadata), this one ALTERs the ``users``
table owned by ``identity_metadata`` (migration ``0001``). Because
``instance_role`` is declared on that ORM table (``identity_orm.py``, the
single source of truth ``0001`` builds via ``create_all``), a FRESH
database already has the column after ``0001``. So this migration only
needs to patch databases created BEFORE the column entered the ORM; the
inspector guard makes both paths converge instead of failing with a
duplicate-column error on a fresh ``upgrade head``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_instance_role"
down_revision = "0008_local_credentials"
branch_labels = None
depends_on = None


def _users_has_instance_role() -> bool:
    """Whether the live ``users`` table already carries ``instance_role``."""
    bind = op.get_bind()
    columns = {col["name"] for col in sa.inspect(bind).get_columns("users")}
    return "instance_role" in columns


def upgrade() -> None:
    if _users_has_instance_role():
        return
    op.add_column(
        "users",
        sa.Column(
            "instance_role",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'user'"),
        ),
    )


def downgrade() -> None:
    if not _users_has_instance_role():
        return
    op.drop_column("users", "instance_role")
