"""Postgres-backed account-preferences store (durable project tier).

A single preferences row per ``(tenant_id, user_id)`` with RLS and the inherited
tenant-session lifecycle (:class:`BaseSessionStore`). The whole-row upsert is
keyed on the composite PK; there is no list, no children, no created_at to
preserve (preferences have no creation lifecycle distinct from their last
save). ``get_preferences`` returns ``None`` when the user has no row yet.
"""

from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.project.account_preferences_ports import AccountPreferences
from inqtrix.project.base_session_store import (
    BaseSessionStore,
    DEFAULT_TENANT as _DEFAULT_TENANT,
)
from inqtrix.storage.account_orm import account_preferences

_MUTABLE = [
    "contrast_mode",
    "locale",
    "theme",
    "theme_preset",
    "user_bubble_tone",
    "enable_agent_memory",
    "chat_model_tier",
    "agent_model_tier",
    "updated_at",
]
"""Every column the upsert may overwrite. A column missing here is written by
the first INSERT and then never again — the failure only shows on the SECOND
save, so additions belong in the same change as the column itself."""


class PostgresAccountPreferencesStore(BaseSessionStore):
    """Durable :class:`~inqtrix.project.account_preferences_ports.AccountPreferencesStore`.

    Inherits the engine + tenant-session lifecycle from
    :class:`~inqtrix.project.base_session_store.BaseSessionStore`.
    """

    async def get_preferences(
        self, *, user_id: uuid.UUID
    ) -> AccountPreferences | None:
        async with self._session() as session:
            row = (await session.execute(select(account_preferences).where(
                account_preferences.c.tenant_id == _DEFAULT_TENANT,
                account_preferences.c.user_id == user_id,
            ))).first()
        return self._from_row(row) if row is not None else None

    async def upsert_preferences(
        self,
        *,
        user_id: uuid.UUID,
        contrast_mode,
        locale,
        theme,
        theme_preset,
        user_bubble_tone,
        updated_at,
        enable_agent_memory=False,
        chat_model_tier="",
        agent_model_tier="",
    ) -> AccountPreferences:
        stmt = pg_insert(account_preferences).values(
            tenant_id=_DEFAULT_TENANT, user_id=user_id, contrast_mode=contrast_mode,
            locale=locale, theme=theme, theme_preset=theme_preset,
            user_bubble_tone=user_bubble_tone,
            enable_agent_memory=enable_agent_memory,
            chat_model_tier=chat_model_tier,
            agent_model_tier=agent_model_tier,
            updated_at=updated_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[account_preferences.c.tenant_id, account_preferences.c.user_id],
            set_={col: getattr(stmt.excluded, col) for col in _MUTABLE},
        ).returning(account_preferences)
        async with self._session() as session:
            row = (await session.execute(stmt)).one()
        return self._from_row(row)

    @staticmethod
    def _from_row(row) -> AccountPreferences:
        return AccountPreferences(
            user_id=row.user_id, contrast_mode=row.contrast_mode, locale=row.locale,
            theme=row.theme, theme_preset=row.theme_preset,
            user_bubble_tone=row.user_bubble_tone,
            enable_agent_memory=row.enable_agent_memory,
            chat_model_tier=row.chat_model_tier,
            agent_model_tier=row.agent_model_tier,
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
        )
