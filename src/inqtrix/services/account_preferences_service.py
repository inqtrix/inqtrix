"""Account-preferences persistence service (project tier).

The thinnest of the persistence services: payload validation and a pass to
the store. There is no owner/share access rule because a caller can only ever
address their own ``user_id`` (the row key is the authenticated principal's
canonical UUID, never a URL/body value) — per-user isolation is structural,
not enforced here.
"""

from __future__ import annotations

import uuid
from inqtrix.model_routing import TIER_NAMES
from inqtrix.project.account_preferences_ports import (
    AccountPreferences,
    AccountPreferencesStore,
)

_VALID_MODEL_TIER = frozenset(TIER_NAMES) | {""}
"""Tier domain for the per-surface model preference, derived from the routing
table rather than re-spelled here — a second literal list would drift from the
tiers the resolver actually knows. ``''`` means "no preference"."""

_VALID_CONTRAST = frozenset({"standard", "high"})
_VALID_LOCALE = frozenset({"de", "en"})
_VALID_THEME = frozenset({"light", "dark", "system"})
_VALID_PRESET = frozenset({"petrol", "standard", "slate", "graphite", "sage"})
_VALID_USER_BUBBLE_TONE = frozenset(
    {"gray", "mint", "orange", "sky", "violet", "ink"}
)


class AccountPreferencesValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400)."""


class AccountPreferencesService:
    """Application service over an :class:`AccountPreferencesStore`."""

    def __init__(self, *, store: AccountPreferencesStore, durable: bool = False) -> None:
        self._store = store
        self._durable = durable

    @property
    def store(self) -> AccountPreferencesStore:
        return self._store

    @property
    def durable(self) -> bool:
        return self._durable

    async def get_preferences(
        self, *, user_id: uuid.UUID
    ) -> AccountPreferences | None:
        return await self._store.get_preferences(user_id=user_id)

    async def save_preferences(
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
        if contrast_mode not in _VALID_CONTRAST:
            raise AccountPreferencesValidationError(f"unknown contrast mode: {contrast_mode!r}")
        if locale not in _VALID_LOCALE:
            raise AccountPreferencesValidationError(f"unknown locale: {locale!r}")
        if theme not in _VALID_THEME:
            raise AccountPreferencesValidationError(f"unknown theme: {theme!r}")
        if theme_preset not in _VALID_PRESET:
            raise AccountPreferencesValidationError(f"unknown theme preset: {theme_preset!r}")
        if user_bubble_tone not in _VALID_USER_BUBBLE_TONE:
            raise AccountPreferencesValidationError(
                f"unknown user bubble tone: {user_bubble_tone!r}"
            )
        if chat_model_tier not in _VALID_MODEL_TIER:
            raise AccountPreferencesValidationError(
                f"unknown chat model tier: {chat_model_tier!r}"
            )
        if agent_model_tier not in _VALID_MODEL_TIER:
            raise AccountPreferencesValidationError(
                f"unknown agent model tier: {agent_model_tier!r}"
            )
        return await self._store.upsert_preferences(
            user_id=user_id, contrast_mode=contrast_mode, locale=locale, theme=theme,
            theme_preset=theme_preset, user_bubble_tone=user_bubble_tone,
            updated_at=updated_at, enable_agent_memory=bool(enable_agent_memory),
            chat_model_tier=chat_model_tier, agent_model_tier=agent_model_tier,
        )
