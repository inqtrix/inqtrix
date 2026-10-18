"""SQLAlchemy Core definitions of the identity schema.

This metadata defines the current runtime identity-table contract. Historical
Alembic revisions keep immutable Core snapshots so a fresh install traverses
the same schema transitions as an upgraded deployment; revision 0045 performs
the canonical-user hard cut and installs the cross-metadata foreign keys.

Schema decisions:

* ``tenant_id`` is ``text`` (matches ``Principal.tenant_id``; v1 runs
  one tenant ``"default"`` per deployment) and exists on every table
  so the RLS tenant defense has one uniform policy shape.
* Permission/role values are ``text`` plus CHECK constraints — never
  native Postgres enums. The ordering lives exclusively in the
  application enums (:mod:`inqtrix.auth.permissions`); a second
  ordering authority in the database would be a split-brain risk.
* Users get a surrogate UUID primary key with ``UNIQUE(tenant_id, issuer,
  subject)``. External subjects bind logins only; every authorization
  relation references ``users.id``.
* Share resources remain polymorphic text identifiers. Recipients are direct
  users referenced by UUID, so a bare external subject can never become an
  authorization key. Permission checks are resource-aware: existing resource
  kinds remain ``view|edit`` while editor documents additionally allow
  ``suggest``.
* ``resource_shares`` revocation is soft (``revoked_at``) with a
  partial unique index on active rows — one active grant per
  (subject, resource), full revocation history retained.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    and_,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from inqtrix.auth.permissions import (
    SHARE_PERMISSIONS_BY_RESOURCE_TYPE,
    WorkspaceRole,
)

identity_metadata = MetaData()

_WORKSPACE_ROLES = ", ".join(f"'{role.value}'" for role in WorkspaceRole)
_SHARE_RESOURCE_TYPES = ", ".join(
    f"'{resource_type}'"
    for resource_type in SHARE_PERMISSIONS_BY_RESOURCE_TYPE
)
_SHARE_RESOURCE_PERMISSION_RULES = " OR ".join(
    (
        f"(resource_type = '{resource_type}' AND permission IN "
        f"({', '.join(repr(permission.value) for permission in permissions)}))"
    )
    for resource_type, permissions in SHARE_PERMISSIONS_BY_RESOURCE_TYPE.items()
)

_UUID_PK = dict(
    primary_key=True,
    server_default=text("gen_random_uuid()"),
)
_CREATED_AT = dict(
    nullable=False,
    server_default=text("now()"),
)


users = Table(
    "users",
    identity_metadata,
    Column("id", UUID(as_uuid=True), **_UUID_PK),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("email", Text, nullable=False),
    Column("email_verified", Boolean, nullable=False, server_default=text("false")),
    Column("display_name", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), **_CREATED_AT),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
    # Instance-wide role for the admin surface (NOT the per-workspace
    # WorkspaceRole). 'admin' may manage users; 'user' is the default.
    # Arrives with revision 0009; existing rows default to 'user'.
    Column(
        "instance_role", Text, nullable=False, server_default=text("'user'")
    ),
    # The user's canonical project namespace (a ``ws_...`` string), adopted
    # from the browser's namespace on first authenticated boot and returned in
    # /api/auth/session so every device scopes the user's project to the SAME
    # namespace (data follows the user across devices). Nullable: NULL until
    # first adopted; arrives with revision 0019. This is a project UI-namespace
    # anchor, NOT an authorization input (auth is created_by_user_id) and NOT the
    # server-side collaboration ``workspaces`` table.
    Column("default_workspace_id", Text, nullable=True),
    # The deletion operation erasing this account (revision 0095). While it
    # is set the account is being erased: no sign-in admits it, it cannot be
    # enabled, and the deletion machine acts as its owner. NULL otherwise.
    Column("erasure_operation_id", Text, nullable=True),
    UniqueConstraint(
        "tenant_id",
        "issuer",
        "subject",
        name="uq_users_tenant_issuer_subject",
    ),
    Index("ix_users_tenant", "tenant_id"),
)

"""Local mirror of IdP-provisioned users (JIT on first login)."""


def active_admin():
    """An admin who can still administer: enabled and not being erased.

    The last-admin invariant counts these, and administrative events wake
    them. An account under erasure is not disabled in the authorization
    sense -- the deletion machine acts as its owner -- but it can never
    administer again, so it never counts as the admin that keeps the
    instance manageable and is never woken as one.
    """
    return and_(
        users.c.instance_role == "admin",
        users.c.disabled_at.is_(None),
        users.c.erasure_operation_id.is_(None),
    )


# Commit-ordered per-user authorization counter, in its OWN table on
# purpose: authorization reads deliberately hold ``FOR SHARE`` on the
# users row (see ``lock_active_users``), and an in-transaction UPDATE of
# that same row would be a share-to-exclusive upgrade — two concurrent
# permission mutations then deadlock. The separate row makes the bump's
# exclusive lock the clean serialization point: per-user commit order,
# no interaction with the users-row locking discipline. SSE frame gates
# read it as a cheap hint; it is never itself an authorization decision.
user_authorization_generations = Table(
    "user_authorization_generations",
    identity_metadata,
    Column("tenant_id", Text, primary_key=True),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "generation",
        BigInteger,
        nullable=False,
        server_default=text("0"),
    ),
)
"""Per-user authorization generation (frame-gate hint)."""


tenant_security_state = Table(
    "tenant_security_state",
    identity_metadata,
    Column("tenant_id", Text, primary_key=True),
)
"""Pure lock row per tenant serializing first/last-admin commands."""


workspaces = Table(
    "workspaces",
    identity_metadata,
    Column("id", UUID(as_uuid=True), **_UUID_PK),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("name", Text, nullable=False),
    Column(
        "created_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), **_CREATED_AT),
    Index("ix_workspaces_tenant", "tenant_id"),
)
"""Collaboration workspaces (the authorization grouping, NOT the
client-supplied UI namespace that the HTTP layer calls workspace_id)."""


workspace_members = Table(
    "workspace_members",
    identity_metadata,
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "workspace_id",
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
    Column("role", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), **_CREATED_AT),
    CheckConstraint(
        f"role IN ({_WORKSPACE_ROLES})", name="ck_workspace_members_role"
    ),
    Index("ix_workspace_members_tenant_user", "tenant_id", "user_id"),
)
"""Workspace membership with the ordered coarse role."""


invitations = Table(
    "invitations",
    identity_metadata,
    Column("id", UUID(as_uuid=True), **_UUID_PK),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "workspace_id",
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("email", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column(
        "invited_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), **_CREATED_AT),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column(
        "accepted_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(f"role IN ({_WORKSPACE_ROLES})", name="ck_invitations_role"),
    Index(
        "uq_invitations_open",
        "workspace_id",
        text("lower(email)"),
        unique=True,
        postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
    ),
)
"""Invitation allowlist for closed registration (one open invitation
per workspace and e-mail; ``invitations_postgres.accept_open_invitations``
accepts them at the invited user's login)."""


resource_shares = Table(
    "resource_shares",
    identity_metadata,
    Column("id", UUID(as_uuid=True), **_UUID_PK),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column(
        "recipient_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=False),
    Column("permission", Text, nullable=False),
    Column("revision", BigInteger, nullable=False, server_default=text("1")),
    Column(
        "granted_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("created_at", DateTime(timezone=True), **_CREATED_AT),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column(
        "revoked_by_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    CheckConstraint(
        _SHARE_RESOURCE_PERMISSION_RULES,
        name="ck_resource_shares_permission",
    ),
    CheckConstraint(
        f"resource_type IN ({_SHARE_RESOURCE_TYPES})",
        name="ck_resource_shares_type",
    ),
    Index(
        "uq_resource_shares_active",
        "tenant_id",
        "recipient_user_id",
        "resource_type",
        "resource_id",
        unique=True,
        postgresql_where=text("revoked_at IS NULL"),
    ),
    Index(
        "ix_resource_shares_recipient_active",
        "tenant_id",
        "recipient_user_id",
        "resource_type",
        postgresql_where=text("revoked_at IS NULL"),
    ),
    Index(
        "ix_resource_shares_resource_active",
        "tenant_id",
        "resource_type",
        "resource_id",
        postgresql_where=text("revoked_at IS NULL"),
    ),
)
"""Direct user-to-resource shares with soft revocation for audit history.

``accepted_at`` gates consent: NULL = pending (granted, awaiting the
recipient's consent, grants nothing); non-NULL = accepted (active, grants
access). ``revoked_at IS NOT NULL`` = inactive (owner-revoked or
recipient-declined/left). The partial unique index keys on
``revoked_at IS NULL`` only, so there is one active row per tuple whether it
is pending or accepted."""


audit_log = Table(
    "audit_log",
    identity_metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("tenant_id", Text, nullable=False, server_default=text("'default'")),
    Column("occurred_at", DateTime(timezone=True), **_CREATED_AT),
    Column(
        "actor_user_id",
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    ),
    Column("actor_type", Text, nullable=False, server_default=text("'user'")),
    Column("action", Text, nullable=False),
    Column("resource_type", Text, nullable=False),
    Column("resource_id", Text, nullable=False),
    Column("workspace_id", UUID(as_uuid=True), nullable=True),
    Column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # Read-model fields (migration 0072, OCSF-oriented): outcome of the
    # action, request origin facts, correlation join keys into logs and
    # traces, and the stable usr_<hex16> pseudonym computed at write
    # time so the admin panel never recomputes HMACs per page.
    Column(
        "outcome", Text, nullable=False, server_default=text("'success'")
    ),
    Column("origin", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column(
        "correlation",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    ),
    Column("actor_pseudonym", Text, nullable=True),
    Index("ix_audit_log_tenant_occurred", "tenant_id", "occurred_at"),
    Index(
        "ix_audit_log_tenant_action_occurred",
        "tenant_id",
        "action",
        "occurred_at",
    ),
    Index(
        "ix_audit_log_tenant_actor_occurred",
        "tenant_id",
        "actor_user_id",
        "occurred_at",
    ),
)
"""Append-only audit trail of semantic actions. The application role
holds INSERT/SELECT only — UPDATE/DELETE are not granted (note: grants
bind the runtime role; table owner and superusers bypass them, so this
is WORM-ish, not compliance-grade WORM). Retention deletes flow through
the SECURITY DEFINER function ``audit_prune(cutoff)`` (migration 0072),
the one sanctioned door through that grant wall."""
