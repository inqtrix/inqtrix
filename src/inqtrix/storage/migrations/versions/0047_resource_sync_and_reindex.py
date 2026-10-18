"""Add user invalidations and mandatory editor revisions.

Revision ID: 0047_resource_sync
Revises: 0046_execution_authority

The event table is a content-free refetch signal, not a patch/outbox payload.
Prompt and skill revisions replace timestamp-based optional OCC with one
mandatory integer compare-and-swap contract.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_resource_sync"
down_revision = "0046_execution_authority"
branch_labels = None
depends_on = None

APP_ROLE = "inqtrix_app"


# Frozen revision-0047 schema. Importing the live user-event ORM would allow a
# later model change to rewrite the historical migration on fresh installs.
_user_event_metadata = sa.MetaData()

_user_events = sa.Table(
    "user_events",
    _user_event_metadata,
    sa.Column(
        "id",
        sa.BigInteger,
        sa.Identity(always=True),
        primary_key=True,
    ),
    sa.Column(
        "tenant_id",
        sa.Text,
        nullable=False,
        server_default=sa.text("'default'"),
    ),
    sa.Column(
        "target_user_id",
        postgresql.UUID(as_uuid=True),
        nullable=False,
    ),
    sa.Column("scope", sa.Text, nullable=False),
    sa.Column("resource_type", sa.Text, nullable=True),
    sa.Column("resource_id", sa.Text, nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    ),
    sa.Index(
        "ix_user_events_tenant_target_id",
        "tenant_id",
        "target_user_id",
        "id",
    ),
)


def upgrade() -> None:
    """Install the authoritative-refetch and revision schema."""
    bind = op.get_bind()
    _user_event_metadata.create_all(bind=bind)
    op.execute(
        "ALTER TABLE user_events ADD CONSTRAINT fk_user_events_target_user "
        "FOREIGN KEY (target_user_id) REFERENCES users(id) "
        "ON DELETE RESTRICT"
    )
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON user_events TO {APP_ROLE}"
    )
    op.execute(
        f"GRANT USAGE, SELECT ON SEQUENCE user_events_id_seq TO {APP_ROLE}"
    )
    op.execute("ALTER TABLE user_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE user_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON user_events
            FOR ALL
            USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
            WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
        """
    )

    op.execute(
        "ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS "
        "revision bigint NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE skill_templates ADD COLUMN IF NOT EXISTS "
        "revision bigint NOT NULL DEFAULT 1"
    )


def downgrade() -> None:
    """Reject downgrade across the irreversible v0.2 identity boundary."""
    raise RuntimeError(
        "0047_resource_sync cannot be downgraded across the irreversible "
        "0045 identity hard cut"
    )
