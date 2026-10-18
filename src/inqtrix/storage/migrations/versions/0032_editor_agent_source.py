"""Editor document source: allow 'agent-artifact' (agent-run export).

Revision ID: 0032_editor_agent_source
Revises: 0031_editor_patches

Extends the ``ck_editor_documents_source`` CHECK from 0014 to admit
``'agent-artifact'`` — the source stamped when a workspace-agent memo is
exported into the editor ("In Editor uebernehmen", API #12). Keeping this
distinct from ``'imported-research-report'`` lets the UI show agent-run
provenance (``source_run_id`` links back to the run) without conflating it
with a native research-report import. Additive-only: existing rows keep
their source; the down-revision restores the 0014 domain (no
``'agent-artifact'`` rows can exist under it, so the older constraint is
safe to reinstate only after such rows are migrated away — the downgrade
therefore first rewrites any ``'agent-artifact'`` to the closest legacy
value ``'imported-research-report'``).
"""

from __future__ import annotations

from alembic import op

revision = "0032_editor_agent_source"
down_revision = "0031_editor_patches"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_editor_documents_source"
_LEGACY = "('blank', 'imported-research-report', 'pasted')"
_EXTENDED = "('blank', 'imported-research-report', 'pasted', 'agent-artifact')"


def upgrade() -> None:
    op.execute(f"ALTER TABLE editor_documents DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE editor_documents ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (source IN {_EXTENDED})"
    )


def downgrade() -> None:
    # Preserve the CHECK invariant: any agent-export rows must fold into a
    # legacy value before the narrower constraint returns.
    op.execute(
        "UPDATE editor_documents SET source = 'imported-research-report' "
        "WHERE source = 'agent-artifact'"
    )
    op.execute(f"ALTER TABLE editor_documents DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE editor_documents ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (source IN {_LEGACY})"
    )
