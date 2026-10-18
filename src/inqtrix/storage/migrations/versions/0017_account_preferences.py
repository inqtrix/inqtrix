"""Account-preferences schema: per-user UI preferences singleton.

Revision ID: 0017_account_preferences
Revises: 0016_vector_index_records

Creates the account-preferences table from its metadata snapshot and
applies the established security layering: DML grants for ``inqtrix_app`` +
ENABLE/FORCE row-level security with the fail-closed tenant policy,
identical to ``0013``..``0016``.

CHECK constraints pin each preference to its frontend union
(``ContrastMode`` / ``Locale`` / ``ThemeMode`` / ``ThemePreset``) so an
out-of-domain write fails loudly at the database boundary.
"""

from __future__ import annotations

from alembic import op

# Frozen schema snapshot from the deployed revision. Historical migrations
# must never import the live ORM because later authority changes would alter
# the schema produced by a fresh traversal.
from sqlalchemy import (
    Column,
    Float,
    MetaData,
    Table,
    Text,
    text,
)

account_metadata = MetaData()

account_preferences = Table(
    "account_preferences",
    account_metadata,
    # COMPOSITE primary key (tenant_id, sub): one preferences row per user.
    # ``sub`` is the principal subject, never a URL/body value, so a caller can
    # only ever address their own row — per-user isolation is structural.
    Column("tenant_id", Text, primary_key=True, server_default=text("'default'")),
    Column("sub", Text, primary_key=True),
    Column("contrast_mode", Text, nullable=False, server_default=text("'standard'")),
    Column("locale", Text, nullable=False, server_default=text("'en'")),
    Column("theme", Text, nullable=False, server_default=text("'system'")),
    Column("theme_preset", Text, nullable=False, server_default=text("'standard'")),
    Column("updated_at", Float, nullable=False),
)
"""One user's account-level UI preferences (theme/locale/contrast), following
the user across workspaces and devices. Upserted as a whole on change; there
is no create/delete lifecycle — the first PUT creates the singleton row."""

revision = "0017_account_preferences"
down_revision = "0016_vector_index_records"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


def upgrade() -> None:
    bind = op.get_bind()
    account_metadata.create_all(bind=bind)

    op.execute(
        "ALTER TABLE account_preferences ADD CONSTRAINT ck_account_preferences_contrast "
        "CHECK (contrast_mode IN ('standard', 'high'))"
    )
    op.execute(
        "ALTER TABLE account_preferences ADD CONSTRAINT ck_account_preferences_locale "
        "CHECK (locale IN ('de', 'en'))"
    )
    op.execute(
        "ALTER TABLE account_preferences ADD CONSTRAINT ck_account_preferences_theme "
        "CHECK (theme IN ('light', 'dark', 'system'))"
    )
    op.execute(
        "ALTER TABLE account_preferences ADD CONSTRAINT ck_account_preferences_preset "
        "CHECK (theme_preset IN ('standard', 'slate', 'graphite', 'sage'))"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON account_preferences TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE account_preferences ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE account_preferences FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON account_preferences
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    account_metadata.drop_all(bind=bind)
