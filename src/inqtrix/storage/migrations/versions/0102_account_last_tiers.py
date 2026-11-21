"""Remember the last report profile and Stufe a user picked.

Revision ID: 0102_account_last_tiers
Revises: 0101_retire_mission_skill_tools

The research composer and the Agent Desk composer each start with a level of
effort: a report profile (``schnell``, ``compact``, ``deep``) and a Stufe
(``schnell``, ``gruendlich``, ``tief``). Both are working habits of the user,
so the last pick belongs beside theme, locale and the model tiers and follows
the user across devices. Two columns, because the two vocabularies differ.

The server stores the picks and derives nothing from them: a run carries the
profile or Stufe its request names.

``''`` means "no pick yet"; the composer then starts on its default. Old rows
and old clients resolve to that value. The rules spell their values out here:
a revision freezes the vocabulary it was written for, and a later revision
that widens one widens its rule.

The lookup that keeps a rule from being added twice is bound to this table,
so a rule of the same name on the table of another schema in the same
database cannot stand in for it.

The downgrade drops the two rules and the two columns, and with them the
saved picks; the composers then start on their defaults again. Back up the
database before the upgrade; the migration command only upgrades.
"""

from __future__ import annotations

from alembic import op

revision = "0102_account_last_tiers"
down_revision = "0101_retire_mission_skill_tools"
branch_labels = None
depends_on = None

_DOMAINS = {
    "last_research_profile": ("", "schnell", "compact", "deep"),
    "last_agent_tier": ("", "schnell", "gruendlich", "tief"),
}


def upgrade() -> None:
    for column, values in _DOMAINS.items():
        literals = ", ".join(f"'{value}'" for value in values)
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
                      AND conrelid = 'account_preferences'::regclass
                ) THEN
                    ALTER TABLE account_preferences
                        ADD CONSTRAINT ck_account_preferences_{column}
                        CHECK ({column} IN ({literals}));
                END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for column in _DOMAINS:
        op.execute(
            f"ALTER TABLE account_preferences "
            f"DROP CONSTRAINT IF EXISTS ck_account_preferences_{column}"
        )
        op.execute(f"ALTER TABLE account_preferences DROP COLUMN IF EXISTS {column}")
