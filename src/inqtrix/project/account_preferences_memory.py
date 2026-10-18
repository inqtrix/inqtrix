"""In-memory account-preferences store (tier without Postgres + offline test).

Mirrors :class:`~inqtrix.project.account_preferences_postgres.PostgresAccountPreferencesStore`
behavior (whole-row upsert keyed on ``user_id``; ``get`` returns ``None`` when
unset). Process-local, not durable.
"""

from __future__ import annotations

import uuid
from inqtrix.project.account_preferences_ports import AccountPreferences


class MemoryAccountPreferencesStore:
    """Process-local :class:`~inqtrix.project.account_preferences_ports.AccountPreferencesStore`."""

    def __init__(self) -> None:
        self._rows: dict[uuid.UUID, AccountPreferences] = {}

    async def get_preferences(
        self, *, user_id: uuid.UUID
    ) -> AccountPreferences | None:
        return self._rows.get(user_id)

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
        prefs = AccountPreferences(
            user_id=user_id, contrast_mode=contrast_mode, locale=locale, theme=theme,
            theme_preset=theme_preset, user_bubble_tone=user_bubble_tone,
            enable_agent_memory=enable_agent_memory,
            chat_model_tier=chat_model_tier,
            agent_model_tier=agent_model_tier,
            updated_at=updated_at,
        )
        self._rows[user_id] = prefs
        return prefs

    async def aclose(self) -> None:
        return None
