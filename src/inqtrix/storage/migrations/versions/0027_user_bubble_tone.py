"""Add user bubble tone to account preferences.

Revision ID: 0027_user_bubble_tone
Revises: 0026_knowledge_session_groups

The Research Desk user-message bubble tone is an account preference, like
theme, locale, and contrast. The column defaults to ``gray`` so old rows and
old clients resolve to the neutral bubble style.
"""

from __future__ import annotations

from alembic import op

revision = "0027_user_bubble_tone"
down_revision = "0026_knowledge_session_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE account_preferences "
        "ADD COLUMN IF NOT EXISTS user_bubble_tone text NOT NULL DEFAULT 'gray'"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'ck_account_preferences_user_bubble_tone'
            ) THEN
                ALTER TABLE account_preferences
                    ADD CONSTRAINT ck_account_preferences_user_bubble_tone
                    CHECK (
                        user_bubble_tone IN (
                            'gray', 'mint', 'orange', 'sky', 'violet', 'ink'
                        )
                    );
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE account_preferences "
        "DROP CONSTRAINT IF EXISTS ck_account_preferences_user_bubble_tone"
    )
    op.execute("ALTER TABLE account_preferences DROP COLUMN IF EXISTS user_bubble_tone")
