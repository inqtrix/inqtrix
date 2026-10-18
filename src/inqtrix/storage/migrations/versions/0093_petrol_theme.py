"""Allow the Petrol theme without changing existing account preferences.

Revision ID: 0093_petrol_theme
Revises: 0092_editor_library_upgrade

The browser owns the new-user default. The database default stays 'standard'
for clients that omit theme_preset. The managed migration runner supplies
exclusive maintenance and RLS authority for the downgrade's cross-user update.
"""

from alembic import op

revision = "0093_petrol_theme"
down_revision = "0092_editor_library_upgrade"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_account_preferences_preset", "account_preferences", type_="check")
    op.create_check_constraint(
        "ck_account_preferences_preset",
        "account_preferences",
        "theme_preset IN ('petrol', 'standard', 'slate', 'graphite', 'sage')",
    )


def downgrade() -> None:
    op.execute("UPDATE account_preferences SET theme_preset = 'standard' WHERE theme_preset = 'petrol'")
    op.drop_constraint("ck_account_preferences_preset", "account_preferences", type_="check")
    op.create_check_constraint(
        "ck_account_preferences_preset",
        "account_preferences",
        "theme_preset IN ('standard', 'slate', 'graphite', 'sage')",
    )
