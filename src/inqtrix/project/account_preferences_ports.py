"""Contracts of the account-preferences store.

The simplest project-persistence port: a single preferences row per user
(``user_id``), no workspace dimension, no list, no children. ``get_preferences``
returns ``None`` when the user has never saved — the caller (router) maps that
to 404 so the frontend keeps its own default theme/locale (the defaults are a
frontend SSOT, never fabricated server-side). Two implementations behind one
port: :class:`~inqtrix.project.account_preferences_memory.MemoryAccountPreferencesStore`
and :class:`~inqtrix.project.account_preferences_postgres.PostgresAccountPreferencesStore`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AccountPreferences:
    """One user's account-level UI preferences.

    Attributes:
        user_id: The owning principal's canonical UUID (the row key).
        contrast_mode: ``standard`` or ``high``.
        locale: ``de`` or ``en``.
        theme: ``light`` / ``dark`` / ``system``.
        theme_preset: ``petrol`` / ``standard`` / ``slate`` / ``graphite`` / ``sage``.
        user_bubble_tone: User-message bubble tone selected in the desk UI.
        enable_agent_memory: Whether the user opted long-term agent memory
            IN. Default ``False`` — memory is off until the user enables it
            (privacy default), so an absent/legacy row means memory-less.
        chat_model_tier: Preferred tier for new chats — ``''`` (no
            preference, deployment default applies), ``high``, ``mid`` or
            ``fast``.
        agent_model_tier: The same for agent runs, deliberately a SEPARATE
            value: an agent run fans out over several thinking nodes while a
            chat answer is a single call, so a chat preference must never
            silently raise agent spend. A tier rather than a model id,
            because this row follows the user across stacks where a concrete
            id may not exist.
        updated_at: Unix timestamp of the last save.
        tenant_id: The tenant scope (RLS).
    """

    user_id: uuid.UUID
    contrast_mode: str
    locale: str
    theme: str
    theme_preset: str
    updated_at: float
    user_bubble_tone: str = "gray"
    enable_agent_memory: bool = False
    chat_model_tier: str = ""
    agent_model_tier: str = ""
    tenant_id: str = "default"


@runtime_checkable
class AccountPreferencesStore(Protocol):
    """Persistence port for the per-user preferences singleton."""

    async def get_preferences(
        self, *, user_id: uuid.UUID
    ) -> AccountPreferences | None:
        """The user's stored preferences, or ``None`` when never saved."""
        ...

    async def upsert_preferences(
        self,
        *,
        user_id: uuid.UUID,
        contrast_mode: str,
        locale: str,
        theme: str,
        theme_preset: str,
        user_bubble_tone: str,
        updated_at: float,
        enable_agent_memory: bool = False,
        chat_model_tier: str = "",
        agent_model_tier: str = "",
    ) -> AccountPreferences:
        """Insert or replace the user's preferences row (whole-row upsert)."""
        ...

    async def aclose(self) -> None: ...
