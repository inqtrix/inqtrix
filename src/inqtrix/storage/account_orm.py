"""SQLAlchemy Core definition of the account-preferences schema.

The account tier of the project-persistence work — and the one piece that is
NOT project data: a user's UI preferences (theme, locale, contrast, bubble tone)
follow the USER, not the (user, workspace) project. So this is a singleton row
per ``(tenant_id, user_id)``, keyed on the canonical local user UUID. Unscoped
anonymous/static deployments do not create account rows. There is no workspace
dimension and no keyset list — there is exactly one row per scoped user.
It is deliberately excluded from the project import/sync (which is
per-(user, workspace)).

Only the genuinely account-level preferences live here. The device-local UI
state (``editorUi`` / ``ui`` — panel widths, scroll, drafts) stays in the
browser's localStorage per the storage matrix and is never sent to the server.

Type decisions match the sibling ORMs: composite PK, unix-seconds ``Float``
``updated_at``, ``tenant_id`` for the RLS layering. CHECK constraints pin each
preference to its frontend union (``ContrastMode`` / ``Locale`` / ``ThemeMode``
/ ``ThemePreset`` / ``UserBubbleTone``) so an out-of-domain write fails loudly
at the DB boundary.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    MetaData,
    Table,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

account_metadata = MetaData()

account_preferences = Table(
    "account_preferences",
    account_metadata,
    # Composite primary key: one preferences row per canonical local user.
    Column("tenant_id", Text, primary_key=True, server_default=text("'default'")),
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("contrast_mode", Text, nullable=False, server_default=text("'standard'")),
    Column("locale", Text, nullable=False, server_default=text("'en'")),
    Column("theme", Text, nullable=False, server_default=text("'system'")),
    Column("theme_preset", Text, nullable=False, server_default=text("'standard'")),
    Column("user_bubble_tone", Text, nullable=False, server_default=text("'gray'")),
    # Long-term agent memory is opt-in per user (privacy default OFF): old rows
    # and old clients resolve to ``false`` and the agent stays memory-less until
    # the user enables it in Settings.
    Column(
        "enable_agent_memory",
        Boolean,
        nullable=False,
        server_default=text("false"),
    ),
    # Preferred model TIER per surface, deliberately one column each: an agent
    # run fans out over several thinking nodes while a chat answer is a single
    # call, so a chat preference must never silently raise agent spend. A tier
    # rather than a model id, because this row follows the user across stacks
    # where a concrete id may not exist. ``''`` means no preference.
    Column("chat_model_tier", Text, nullable=False, server_default=text("''")),
    Column("agent_model_tier", Text, nullable=False, server_default=text("''")),
    Column("updated_at", Float, nullable=False),
)
"""One user's account-level UI preferences, following
the user across workspaces and devices. Upserted as a whole on change; there
is no create/delete lifecycle — the first PUT creates the singleton row."""
