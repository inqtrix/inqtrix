"""Add the per-user authorization generation table.

Revision ID: 0079_authorization_generation
Revises: 0078_chat_thread_model_selection

A commit-ordered per-user counter for authorization changes, in its OWN
table: authorization reads deliberately hold ``FOR SHARE`` on the users
row (``lock_active_users``), so an in-transaction UPDATE of that row
would be a share-to-exclusive upgrade — two concurrent permission
mutations then deadlock. The dedicated row makes the bump's exclusive
lock the serialization point instead: every permission-relevant
mutation (share revoked/changed, workspace member removed, user
disabled, password reset, session deleted, PAT revoked) upserts
``generation + 1`` INSIDE its own transaction, so the value is
commit-ordered per user — unlike a sequence, whose values are assigned
at ``nextval()`` and can surface out of commit order. Long-lived SSE
streams read the counter as a cheap per-frame hint and re-run the full
authorization chain only when it moved (or a bounded time ceiling
elapsed). Never itself an authorization decision.
"""

from __future__ import annotations

from alembic import op

revision = "0079_authorization_generation"
down_revision = "0078_chat_thread_model_selection"
branch_labels = None
depends_on = None


APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS user_authorization_generations ("
        "tenant_id text NOT NULL, "
        "user_id uuid NOT NULL, "
        "generation bigint NOT NULL DEFAULT 0, "
        "PRIMARY KEY (tenant_id, user_id))"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE "
        f"ON user_authorization_generations TO {APP_ROLE}"
    )
    op.execute(
        "ALTER TABLE user_authorization_generations "
        "ENABLE ROW LEVEL SECURITY"
    )
    op.execute(
        "ALTER TABLE user_authorization_generations "
        "FORCE ROW LEVEL SECURITY"
    )
    op.execute(
        """
        CREATE POLICY tenant_isolation ON user_authorization_generations
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS user_authorization_generations")
