"""Identity schema: users, workspaces, groups, shares, audit + RLS.

Revision ID: 0001_identity_schema
Revises: None

Creates the identity tables from the Core metadata (single source of
truth, zero DDL drift) and layers on the security primitives that
SQLAlchemy cannot express:

* the ``inqtrix_app`` role — NOLOGIN/NOSUPERUSER/NOBYPASSRLS, the
  role every application transaction switches to via ``SET LOCAL
  ROLE`` so row-level security applies even when the connection user
  is the table owner or a superuser. NOLOGIN keeps the dev stack free
  of a second password; production may create a LOGIN role that
  inherits it.
* ``inqtrix_current_tenant_id()`` — the fail-closed tenant resolver.
  Raises (ERRCODE 28000) when the ``inqtrix.tenant_id`` GUC is unset
  OR empty; the empty check matters because a transaction-locally set
  GUC survives as an empty string for the connection lifetime after
  the transaction ends, which would otherwise silently match nothing.
* ENABLE + FORCE row-level security on every tenant table with one
  uniform policy. The ``(SELECT ...)`` wrapper around the helper call
  is load-bearing: it makes the planner evaluate the function once
  per query (InitPlan) instead of once per row.
* Grants: full DML for ``inqtrix_app`` on the identity tables, but
  INSERT/SELECT only on ``audit_log`` (append-only enforced by
  grants — WORM-ish, not compliance-grade WORM; owners and superusers
  bypass grants).

Downgrade drops the schema objects but leaves the ``inqtrix_app``
role in place: roles are cluster-wide and may be referenced by other
databases; the upgrade recreates it idempotently.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_identity_schema"
down_revision = None
branch_labels = None
depends_on = None


TENANT_TABLES = (
    "users",
    "workspaces",
    "workspace_members",
    "groups",
    "group_members",
    "invitations",
    "resource_shares",
    "audit_log",
)

APP_ROLE = "inqtrix_app"


# Migration metadata is an immutable snapshot of the schema at revision 0001.
# Importing the live ORM here would make fresh installs skip the legacy subject
# columns and local-group tables that revision 0045 is responsible for removing.
_identity_metadata = sa.MetaData()

_UUID_PK = {
    "primary_key": True,
    "server_default": sa.text("gen_random_uuid()"),
}
_CREATED_AT = {
    "nullable": False,
    "server_default": sa.text("now()"),
}

_users = sa.Table(
    "users",
    _identity_metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), **_UUID_PK),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("issuer", sa.Text, nullable=False),
    sa.Column("subject", sa.Text, nullable=False),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column(
        "email_verified", sa.Boolean, nullable=False, server_default=sa.text("false")
    ),
    sa.Column("display_name", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    sa.Index("ix_users_tenant", "tenant_id"),
)

_workspaces = sa.Table(
    "workspaces",
    _identity_metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), **_UUID_PK),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("created_by_sub", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Index("ix_workspaces_tenant", "tenant_id"),
)

_workspace_members = sa.Table(
    "workspace_members",
    _identity_metadata,
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column(
        "workspace_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("sub", sa.Text, primary_key=True),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.CheckConstraint(
        "role IN ('viewer', 'commenter', 'editor', 'owner')",
        name="ck_workspace_members_role",
    ),
    sa.Index("ix_workspace_members_tenant_sub", "tenant_id", "sub"),
)

_groups = sa.Table(
    "groups",
    _identity_metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), **_UUID_PK),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Index("ix_groups_tenant", "tenant_id"),
)

_group_members = sa.Table(
    "group_members",
    _identity_metadata,
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column(
        "group_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("sub", sa.Text, primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Index("ix_group_members_tenant_sub", "tenant_id", "sub"),
)

_invitations = sa.Table(
    "invitations",
    _identity_metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), **_UUID_PK),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column(
        "workspace_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("email", sa.Text, nullable=False),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("invited_by_sub", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("accepted_by_sub", sa.Text, nullable=True),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint(
        "role IN ('viewer', 'commenter', 'editor', 'owner')",
        name="ck_invitations_role",
    ),
    sa.Index(
        "uq_invitations_open",
        "workspace_id",
        sa.text("lower(email)"),
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL AND revoked_at IS NULL"),
    ),
)

_resource_shares = sa.Table(
    "resource_shares",
    _identity_metadata,
    sa.Column("id", postgresql.UUID(as_uuid=True), **_UUID_PK),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("subject_type", sa.Text, nullable=False),
    sa.Column("subject_id", sa.Text, nullable=False),
    sa.Column("resource_type", sa.Text, nullable=False),
    sa.Column("resource_id", sa.Text, nullable=False),
    sa.Column("permission", sa.Text, nullable=False),
    sa.Column("granted_by_sub", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("revoked_by_sub", sa.Text, nullable=True),
    sa.CheckConstraint(
        "subject_type IN ('user', 'group')",
        name="ck_resource_shares_subject",
    ),
    sa.CheckConstraint(
        "permission IN ('view', 'comment', 'edit', 'manage')",
        name="ck_resource_shares_permission",
    ),
    sa.Index(
        "uq_resource_shares_active",
        "tenant_id",
        "subject_type",
        "subject_id",
        "resource_type",
        "resource_id",
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    ),
    sa.Index(
        "ix_resource_shares_subject_active",
        "tenant_id",
        "subject_type",
        "subject_id",
        "resource_type",
        postgresql_where=sa.text("revoked_at IS NULL"),
    ),
    sa.Index(
        "ix_resource_shares_resource_active",
        "tenant_id",
        "resource_type",
        "resource_id",
        postgresql_where=sa.text("revoked_at IS NULL"),
    ),
)

_audit_log = sa.Table(
    "audit_log",
    _identity_metadata,
    sa.Column(
        "id", sa.BigInteger, sa.Identity(always=True), primary_key=True
    ),
    sa.Column(
        "tenant_id", sa.Text, nullable=False, server_default=sa.text("'default'")
    ),
    sa.Column("occurred_at", sa.DateTime(timezone=True), **_CREATED_AT),
    sa.Column("actor_sub", sa.Text, nullable=False),
    sa.Column(
        "actor_type", sa.Text, nullable=False, server_default=sa.text("'user'")
    ),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("resource_type", sa.Text, nullable=False),
    sa.Column("resource_id", sa.Text, nullable=False),
    sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    sa.Column(
        "detail",
        postgresql.JSONB,
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    ),
    sa.Index("ix_audit_log_tenant_occurred", "tenant_id", "occurred_at"),
)

_CREATE_APP_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS
            NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;
"""

_CREATE_TENANT_FUNCTION = """
CREATE OR REPLACE FUNCTION inqtrix_current_tenant_id() RETURNS text
LANGUAGE plpgsql STABLE PARALLEL SAFE AS $$
DECLARE
    v text := current_setting('inqtrix.tenant_id', true);
BEGIN
    IF v IS NULL OR v = '' THEN
        RAISE EXCEPTION
            'inqtrix.tenant_id is not set; refusing the tenant-scoped '
            'query (use tenant_session)'
            USING ERRCODE = '28000';
    END IF;
    RETURN v;
END
$$;
"""


def upgrade() -> None:
    bind = op.get_bind()
    _identity_metadata.create_all(bind=bind)

    op.execute(_CREATE_APP_ROLE)
    # Membership lets a non-superuser migration/login user SET ROLE to
    # the restricted app role (superusers may anyway).
    op.execute(
        f"""
        DO $$
        BEGIN
            IF current_user <> '{APP_ROLE}' THEN
                EXECUTE format('GRANT {APP_ROLE} TO %I', current_user);
            END IF;
        END
        $$;
        """
    )
    op.execute(_CREATE_TENANT_FUNCTION)

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    for table in TENANT_TABLES:
        if table == "audit_log":
            op.execute(f"GRANT SELECT, INSERT ON audit_log TO {APP_ROLE}")
        else:
            op.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}"
            )
    # The audit_log identity column draws from a sequence; INSERT
    # needs USAGE on it.
    op.execute(f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                FOR ALL
                USING (tenant_id = (SELECT inqtrix_current_tenant_id()))
                WITH CHECK (tenant_id = (SELECT inqtrix_current_tenant_id()))
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Policies drop with their tables; grants on dropped tables vanish.
    _identity_metadata.drop_all(bind=bind)
    op.execute("DROP FUNCTION IF EXISTS inqtrix_current_tenant_id()")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    # The role itself stays (cluster-wide object, idempotent upgrade).
