"""Add per-mode model-tier defaults to account preferences.

Revision ID: 0077_account_model_tier_defaults
Revises: 0076_editor_private_drafts

The model a user prefers is a working habit, not a property of a document or
a project, so it belongs beside theme, locale, and contrast.

Two columns, one per surface that owns a model picker: chat and agent. They
stay SEPARATE for the same reason the client keeps the selections separate
(``apps/research-desk/src/features/project/types.ts``): an agent run fans out
over several thinking nodes while a chat answer is a single call, so a chat
preference must never silently raise agent spend.

The stored value is a TIER, never a concrete model id. A model id is only
meaningful inside one provider stack, while this row follows the user across
workspaces and devices — a stored id would eventually point at nothing. A tier
always resolves, because the routing table names exactly one model per tier.

``''`` means "no preference"; the deployment default then applies, which is the
same thing the picker shows as its server-default entry today. Old rows and old
clients resolve to that value.
"""

from __future__ import annotations

from alembic import op

revision = "0077_account_model_tier_defaults"
down_revision = "0076_editor_private_drafts"
branch_labels = None
depends_on = None

_COLUMNS = ("chat_model_tier", "agent_model_tier")


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE account_preferences "
            f"ADD COLUMN IF NOT EXISTS {column} text NOT NULL DEFAULT ''"
        )
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'ck_account_preferences_{column}'
                ) THEN
                    ALTER TABLE account_preferences
                        ADD CONSTRAINT ck_account_preferences_{column}
                        CHECK ({column} IN ('', 'high', 'mid', 'fast'));
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE account_preferences "
            f"DROP CONSTRAINT IF EXISTS ck_account_preferences_{column}"
        )
        op.execute(f"ALTER TABLE account_preferences DROP COLUMN IF EXISTS {column}")
