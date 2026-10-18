"""Add the per-thread model selection to chat threads.

Revision ID: 0078_chat_thread_model_selection
Revises: 0077_account_model_tier_defaults

The model picked inside a chat sticks to THAT chat: a reload returns to it
instead of falling back to the account preference. The column carries
client-owned JSON exactly like ``agent_sessions.items_json`` — the client
normalizes defensively on read, the server only length-caps the string.
``''`` means "nothing picked here"; the account preference then seeds the
composer, so old rows and old clients keep today's behavior.
"""

from __future__ import annotations

from alembic import op

revision = "0078_chat_thread_model_selection"
down_revision = "0077_account_model_tier_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE chat_threads "
        "ADD COLUMN IF NOT EXISTS model_selection text NOT NULL DEFAULT ''"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE chat_threads DROP COLUMN IF EXISTS model_selection")
