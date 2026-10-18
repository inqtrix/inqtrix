"""Repair the agent-memory RLS policies to read the correct tenant GUC.

Revision ID: 0036_agent_memory_rls_guc
Revises: 0035_children_wait

Migrations 0033/0034 created ``tenant_isolation`` on
``agent_memory_candidates`` and ``agent_feedback`` gated on
``current_setting('app.tenant_id', true)`` — a GUC the application never
sets. The app sets ``inqtrix.tenant_id`` (``storage.db.TENANT_GUC``) and
every other tenant table isolates via the fail-closed helper
``inqtrix_current_tenant_id()`` (defined in 0001, used verbatim in 0030).
Under ``FORCE ROW LEVEL SECURITY`` and the ``NOBYPASSRLS`` ``inqtrix_app``
role, the wrong GUC resolves to NULL, so ``tenant_id = NULL`` matched no
row: both tables silently returned and accepted zero rows on Postgres.

This is a fresh corrective migration rather than an in-place edit of
0033/0034: a database that already ran those migrations would not re-run
them (and their ``IF NOT EXISTS`` guard would skip a re-create). ``DROP
POLICY IF EXISTS`` + ``CREATE POLICY`` here repairs already-migrated and
fresh installs alike, matching the 0030 policy byte-for-byte.
"""

from __future__ import annotations

from alembic import op

revision = "0036_agent_memory_rls_guc"
down_revision = "0035_children_wait"
branch_labels = None
depends_on = None

_TABLES = ("agent_memory_candidates", "agent_feedback")


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    # Restore the exact pre-0036 (buggy) policy so the migration chain is
    # symmetric; the GUC name is the historical value 0033/0034 installed.
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true))
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true))
            """
        )
