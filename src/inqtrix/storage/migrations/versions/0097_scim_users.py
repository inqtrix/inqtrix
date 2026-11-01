"""Carry SCIM provisioning state and the source of a disable on accounts.

Revision ID: 0097_scim_users
Revises: 0096_orphaned_source_deletion

SCIM 2.0 provisioning (Users only) manages the accounts of the gateway's
identity source. Four nullable columns on ``users`` hold its state; every
existing account takes NULL, so nothing is backfilled.

* ``scim_user_name`` is the SCIM ``userName``. A partial unique index keeps
  it unique per tenant and issuer without regard to case; accounts SCIM
  never bound stay out of the index.
* ``scim_name`` is the SCIM ``name`` object (``givenName``, ``familyName``,
  ``formatted``).
* ``scim_deleted_at`` marks a tombstone: the identity provider deleted the
  account, and neither a sign-in nor provisioning brings it back.
* ``disabled_source`` records who disabled the account, ``admin`` or
  ``provisioning``. NULL counts as ``admin``: every disable before this
  revision came from an administrator.

The downgrade is lossless only without SCIM state. It counts, over every
tenant, the rows that carry any of it and refuses with the numbers alone;
``disabled_source = 'admin'`` is not SCIM state, because dropping it leaves
NULL, which means the same. Dropping SCIM state instead would silently change
how accounts map to identities: a later upgrade would let provisioning adopt
a former tombstone, and a disable from provisioning would turn into one the
identity provider can no longer lift.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0097_scim_users"
down_revision = "0096_orphaned_source_deletion"
branch_labels = None
depends_on = None

_SCIM_STATE_COUNTS_SQL = """
SELECT
    count(*) FILTER (WHERE scim_user_name IS NOT NULL) AS scim_user_names,
    count(*) FILTER (WHERE scim_name IS NOT NULL) AS scim_names,
    count(*) FILTER (WHERE scim_deleted_at IS NOT NULL) AS tombstones,
    count(*) FILTER (WHERE disabled_source = 'provisioning')
        AS provisioning_disables
FROM users
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("scim_user_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("scim_name", postgresql.JSONB(), nullable=True))
    op.add_column(
        "users",
        sa.Column("scim_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("users", sa.Column("disabled_source", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_users_disabled_source",
        "users",
        "disabled_source IN ('admin', 'provisioning')",
    )
    op.create_index(
        "uq_users_scim_user_name",
        "users",
        ["tenant_id", "issuer", sa.text("lower(scim_user_name)")],
        unique=True,
        postgresql_where=sa.text("scim_user_name IS NOT NULL"),
    )


def downgrade() -> None:
    counts = op.get_bind().execute(sa.text(_SCIM_STATE_COUNTS_SQL)).mappings().one()
    if any(counts.values()):
        raise RuntimeError(
            "SCIM users downgrade blocked: "
            f"scim_user_names={counts['scim_user_names']} "
            f"scim_names={counts['scim_names']} "
            f"tombstones={counts['tombstones']} "
            f"provisioning_disables={counts['provisioning_disables']}. "
            "Dropping this state would change how accounts map to identities. "
            "Restore the matching pre-upgrade database backup instead."
        )
    op.drop_index("uq_users_scim_user_name", table_name="users")
    op.drop_constraint("ck_users_disabled_source", "users", type_="check")
    op.drop_column("users", "disabled_source")
    op.drop_column("users", "scim_deleted_at")
    op.drop_column("users", "scim_name")
    op.drop_column("users", "scim_user_name")
