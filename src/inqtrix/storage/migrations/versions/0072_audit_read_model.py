"""Audit read model: outcome, origin, correlation, pseudonym, prune.

Revision ID: 0072_audit_read_model
Revises: 0071_asset_section_roles

This revision turns ``audit_log`` from a write-
only trail into the admin-facing index (OCSF-oriented field canon):

* ``outcome`` — success | failure | denied. Existing rows default to
  ``success``: every historical writer only recorded successful actions
  (denials carried the ``authz.denied`` action instead).
* ``origin`` JSONB — request origin facts (ip, user_agent, auth_method).
  Deliberately schemaless: transports differ per surface and absence is
  meaningful (worker-side events have no request origin).
* ``correlation`` JSONB — request_id / run_id / trace_id, the join keys
  into JSON logs and traces ("one drill-down path per incident").
* ``actor_pseudonym`` — the stable ``usr_<hex16>`` reference computed at
  WRITE time, so the admin panel lists exactly the identifier that
  appears in logs/traces without recomputing HMACs per page.

Read-path indexes follow the panel's two dominant filters (action
timeline, actor timeline). Retention: ``audit_prune(cutoff)`` runs as
SECURITY DEFINER because the application role deliberately holds
INSERT/SELECT only (WORM-ish contract, migration 0049) — the function is
the ONE sanctioned deletion door and returns only a count. Tenant
scope follows the function OWNER: an RLS-exempt owner (bundled
superuser, BYPASSRLS migration role) prunes across tenants; under
``INQTRIX_MIGRATION_RLS_MODE=owner`` FORCE RLS binds the owner too and
the prune covers only the calling session's tenant (equivalent in the
current single-tenant deployments; revisit before multi-tenant).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0072_audit_read_model"
down_revision = "0071_asset_section_roles"
branch_labels = None
depends_on = None

TABLE = "audit_log"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "outcome",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'success'"),
        ),
    )
    op.create_check_constraint(
        "ck_audit_log_outcome",
        TABLE,
        "outcome IN ('success', 'failure', 'denied')",
    )
    op.add_column(
        TABLE,
        sa.Column(
            "origin",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        TABLE,
        sa.Column(
            "correlation",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        TABLE,
        sa.Column("actor_pseudonym", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_audit_log_tenant_action_occurred",
        TABLE,
        ["tenant_id", "action", "occurred_at"],
    )
    op.create_index(
        "ix_audit_log_tenant_actor_occurred",
        TABLE,
        ["tenant_id", "actor_user_id", "occurred_at"],
    )
    # The one sanctioned deletion door through the INSERT/SELECT-only
    # grant wall: owned by the (RLS-exempt) migration role, executable
    # by the app role, returns only the pruned row count. search_path is
    # pinned — mandatory hygiene for SECURITY DEFINER.
    op.execute(
        """
        CREATE FUNCTION audit_prune(cutoff timestamptz)
        RETURNS bigint
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            pruned bigint;
        BEGIN
            DELETE FROM audit_log WHERE occurred_at < cutoff;
            GET DIAGNOSTICS pruned = ROW_COUNT;
            RETURN pruned;
        END;
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION audit_prune(timestamptz) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION audit_prune(timestamptz) TO inqtrix_app"
    )


def downgrade() -> None:
    raise RuntimeError(
        "This migration is irreversible: the audit read model (outcome, "
        "origin, correlation, pseudonym) is part of the durable audit "
        "contract. Restore the matching pre-upgrade backup instead."
    )
