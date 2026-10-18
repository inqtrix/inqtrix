"""Behavior tests for the account-preferences service (offline tier)."""

from __future__ import annotations

import uuid

import pytest

from inqtrix.project.account_preferences_memory import MemoryAccountPreferencesStore
from inqtrix.services.account_preferences_service import (
    AccountPreferencesService,
    AccountPreferencesValidationError,
)


@pytest.fixture()
def service() -> AccountPreferencesService:
    return AccountPreferencesService(store=MemoryAccountPreferencesStore(), durable=False)


async def _save(
    service,
    user_id,
    *,
    theme="dark",
    locale="de",
    contrast="high",
    preset="slate",
    user_bubble_tone="gray",
    enable_agent_memory=False,
    chat_model_tier="",
    agent_model_tier="",
    updated_at=1.0,
):
    return await service.save_preferences(
        user_id=user_id,
        contrast_mode=contrast,
        locale=locale,
        theme=theme,
        theme_preset=preset, user_bubble_tone=user_bubble_tone,
        enable_agent_memory=enable_agent_memory,
        chat_model_tier=chat_model_tier,
        agent_model_tier=agent_model_tier,
        updated_at=updated_at,
    )


USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")


@pytest.mark.asyncio
async def test_get_returns_none_when_unset(service) -> None:
    assert await service.get_preferences(user_id=USER) is None


@pytest.mark.asyncio
async def test_save_then_get_roundtrip(service) -> None:
    await _save(
        service,
        USER,
        theme="dark",
        locale="de",
        contrast="high",
        preset="sage",
        user_bubble_tone="mint",
    )
    prefs = await service.get_preferences(user_id=USER)
    assert prefs is not None
    assert (
        prefs.theme,
        prefs.locale,
        prefs.contrast_mode,
        prefs.theme_preset,
        prefs.user_bubble_tone,
    ) == ("dark", "de", "high", "sage", "mint")


@pytest.mark.asyncio
async def test_save_is_whole_row_upsert(service) -> None:
    await _save(
        service,
        USER,
        theme="dark",
        preset="slate",
        user_bubble_tone="mint",
        updated_at=1.0,
    )
    await _save(
        service,
        USER,
        theme="light",
        preset="graphite",
        user_bubble_tone="orange",
        updated_at=2.0,
    )
    prefs = await service.get_preferences(user_id=USER)
    assert (prefs.theme, prefs.theme_preset, prefs.user_bubble_tone, prefs.updated_at) == (
        "light", "graphite", "orange", 2.0
    )


@pytest.mark.asyncio
async def test_agent_memory_opt_in_defaults_off_and_round_trips(service) -> None:
    # Privacy default OFF when omitted; opt-in persists and survives the
    # whole-row upsert of another preference.
    await _save(service, USER)
    assert (
        await service.get_preferences(user_id=USER)
    ).enable_agent_memory is False

    await _save(service, USER, enable_agent_memory=True, updated_at=2.0)
    assert (
        await service.get_preferences(user_id=USER)
    ).enable_agent_memory is True

    await _save(service, USER, theme="light", updated_at=3.0)
    assert (
        await service.get_preferences(user_id=USER)
    ).enable_agent_memory is False


@pytest.mark.asyncio
async def test_per_user_isolation(service) -> None:
    await _save(service, USER_A, theme="dark")
    await _save(service, USER_B, theme="light")
    assert (await service.get_preferences(user_id=USER_A)).theme == "dark"
    assert (await service.get_preferences(user_id=USER_B)).theme == "light"


@pytest.mark.asyncio
async def test_validation_rejects_out_of_domain(service) -> None:
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, theme="neon")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, locale="fr")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, contrast="ultra")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, preset="neon")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, user_bubble_tone="rainbow")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, chat_model_tier="turbo")
    with pytest.raises(AccountPreferencesValidationError):
        await _save(service, USER, agent_model_tier="turbo")


@pytest.mark.asyncio
async def test_model_tier_defaults_to_no_preference_and_round_trips(service) -> None:
    await _save(service, USER)
    stored = await service.get_preferences(user_id=USER)
    assert stored.chat_model_tier == ""
    assert stored.agent_model_tier == ""

    await _save(service, USER, chat_model_tier="mid", agent_model_tier="fast")
    stored = await service.get_preferences(user_id=USER)
    assert stored.chat_model_tier == "mid"
    assert stored.agent_model_tier == "fast"


@pytest.mark.asyncio
async def test_chat_tier_never_carries_over_to_the_agent(service) -> None:
    """The two surfaces stay independent all the way down to storage.

    An agent run fans out over several thinking nodes while a chat answer is a
    single call. The client keeps the selections apart for that reason; if the
    preference row merged them, a chat pick would raise agent spend the moment
    it synced.
    """
    await _save(service, USER, chat_model_tier="high")
    stored = await service.get_preferences(user_id=USER)
    assert stored.chat_model_tier == "high"
    assert stored.agent_model_tier == ""


def test_router_reads_an_absent_or_null_tier_as_no_preference() -> None:
    """A missing key and an explicit ``null`` both mean "no preference".

    The router reads every other field through ``str(body.get(key, default))``.
    Copying that pattern here would turn JSON ``null`` into the string
    ``'None'`` — a value the service rejects, so an old client omitting the
    field would get a 400 on an otherwise valid save.
    """
    from inqtrix.server.routers.account_preferences import _tier

    assert _tier({}, "chat_model_tier") == ""
    assert _tier({"chat_model_tier": None}, "chat_model_tier") == ""
    assert _tier({"chat_model_tier": "mid"}, "chat_model_tier") == "mid"
    assert _tier({"chat_model_tier": None}, "chat_model_tier") != "None"


@pytest.mark.asyncio
async def test_model_tier_domain_follows_the_routing_table(service) -> None:
    """Every tier the resolver knows is accepted — no hand-maintained copy.

    A second literal list would drift from the tiers routing actually resolves,
    and the drift would only surface as a rejected save for a tier that works
    everywhere else.
    """
    from inqtrix.model_routing import TIER_NAMES

    for tier in TIER_NAMES:
        await _save(service, USER, chat_model_tier=tier, agent_model_tier=tier)
        stored = await service.get_preferences(user_id=USER)
        assert stored.chat_model_tier == tier
        assert stored.agent_model_tier == tier


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["petrol", "standard", "slate", "graphite", "sage"])
async def test_all_theme_presets_roundtrip(service, preset) -> None:
    await _save(service, USER, preset=preset)
    assert (await service.get_preferences(user_id=USER)).theme_preset == preset
