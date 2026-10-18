"""Add the per-user prompt-default seed marker table.

Revision ID: 0082_prompt_seed_markers
Revises: 0081_editor_markdown_read

One row per (tenant, user) recording that the stock prompt templates
(Lektor, Sprechzettel, Summarizer, Translator) were offered to that user
exactly once. The marker is claimed ATOMICALLY with the template inserts
in one transaction at the user's first template listing — so concurrent
first requests cannot double-seed, a crash cannot strand a claimed
marker without templates, and a user who deletes a default keeps it
deleted forever (the marker is never cleared). A user who already owns
templates at claim time (e.g. rows pushed up by the client project sync
before the first listing) gets the marker WITHOUT inserts — stock
content never injects into a grown library.
"""

from __future__ import annotations

from alembic import op

revision = "0082_prompt_seed_markers"
down_revision = "0081_editor_markdown_read"
branch_labels = None
depends_on = None


APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS prompt_template_seed_markers ("
        "tenant_id text NOT NULL, "
        "user_id uuid NOT NULL, "
        "seeded_at double precision NOT NULL, "
        "PRIMARY KEY (tenant_id, user_id))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON prompt_template_seed_markers TO {APP_ROLE}"
    )
    op.execute(
        "ALTER TABLE prompt_template_seed_markers "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE prompt_template_seed_markers "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation ON prompt_template_seed_markers
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS prompt_template_seed_markers")
