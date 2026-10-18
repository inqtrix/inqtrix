"""Postgres integration tests for the account-preferences store (gated)."""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.project.account_preferences_postgres import PostgresAccountPreferencesStore
from inqtrix.storage.account_orm import account_preferences
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import users
from inqtrix.storage.migrate import run_migrations

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER = uuid.UUID("17171717-1717-4717-8717-171717171717")
USER_A = uuid.UUID("17171717-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
USER_B = uuid.UUID("17171717-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
TEST_USERS = (USER, USER_A, USER_B)


@pytest.fixture(scope="session", autouse=True)
def account_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def store():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail("INQTRIX_TEST_DATABASE_URL must connect as superuser/BYPASSRLS.")
            await session.execute(account_preferences.delete())
            for user_id in TEST_USERS:
                label = f"account-preferences-pg-{user_id.hex}"
                await session.execute(
                    pg_insert(users)
                    .values(
                        id=user_id,
                        tenant_id="default",
                        issuer="https://account-preferences-tests.example",
                        subject=label,
                        email=f"{label}@example.com",
                    )
                    .on_conflict_do_update(
                        index_elements=(users.c.id,),
                        set_={
                            "tenant_id": "default",
                            "issuer": "https://account-preferences-tests.example",
                            "subject": label,
                            "email": f"{label}@example.com",
                            "disabled_at": None,
                        },
                    )
                )
    prefs_store = PostgresAccountPreferencesStore(engine=engine, app_role=APP_ROLE)
    yield prefs_store
    await prefs_store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", ["petrol", "standard", "slate", "graphite", "sage"])
async def test_get_none_then_upsert_roundtrip(store, preset) -> None:
    assert await store.get_preferences(user_id=USER) is None
    await store.upsert_preferences(
        user_id=USER, contrast_mode="high", locale="de", theme="dark",
        theme_preset=preset, user_bubble_tone="mint", updated_at=1.0,
    )
    prefs = await store.get_preferences(user_id=USER)
    assert (
        prefs.theme,
        prefs.locale,
        prefs.contrast_mode,
        prefs.theme_preset,
        prefs.user_bubble_tone,
    ) == (
        "dark", "de", "high", preset, "mint"
    )


@pytest.mark.asyncio
async def test_upsert_replaces_row_for_same_user(store) -> None:
    await store.upsert_preferences(
        user_id=USER, contrast_mode="standard", locale="en", theme="dark",
        theme_preset="slate", user_bubble_tone="mint", updated_at=1.0,
    )
    await store.upsert_preferences(
        user_id=USER, contrast_mode="high", locale="de", theme="light",
        theme_preset="graphite", user_bubble_tone="orange", updated_at=2.0,
    )
    prefs = await store.get_preferences(user_id=USER)
    assert (prefs.theme, prefs.theme_preset, prefs.user_bubble_tone, prefs.updated_at) == (
        "light", "graphite", "orange", 2.0
    )
    # Still a singleton: the upsert updated in place, no second row.
    async with store._session() as session:
        from sqlalchemy import func, select

        count = (await session.execute(
            select(func.count()).select_from(account_preferences)
        )).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_second_save_overwrites_the_model_tiers(store) -> None:
    """The tiers must survive a SECOND upsert, not just the first INSERT.

    A column missing from the ON CONFLICT update set is written once and then
    silently frozen — the user changes their preference, the request succeeds,
    and nothing changes. Only a second save exposes that.
    """
    await store.upsert_preferences(
        user_id=USER, contrast_mode="standard", locale="en", theme="dark",
        theme_preset="slate", user_bubble_tone="mint", updated_at=1.0,
        chat_model_tier="fast", agent_model_tier="fast",
    )
    await store.upsert_preferences(
        user_id=USER, contrast_mode="standard", locale="en", theme="dark",
        theme_preset="slate", user_bubble_tone="mint", updated_at=2.0,
        chat_model_tier="high", agent_model_tier="mid",
    )
    prefs = await store.get_preferences(user_id=USER)
    assert (prefs.chat_model_tier, prefs.agent_model_tier) == ("high", "mid")


@pytest.mark.asyncio
async def test_model_tier_check_constraint_rejects_unknown_tier(store) -> None:
    """The database refuses an out-of-domain tier even if the service is bypassed."""
    from sqlalchemy.exc import DBAPIError, IntegrityError

    with pytest.raises((IntegrityError, DBAPIError)):
        await store.upsert_preferences(
            user_id=USER, contrast_mode="standard", locale="en", theme="dark",
            theme_preset="slate", user_bubble_tone="mint", updated_at=1.0,
            chat_model_tier="turbo",
        )


@pytest.mark.asyncio
async def test_model_tiers_default_to_no_preference_for_legacy_rows(store) -> None:
    """A save that predates the feature leaves both tiers empty, never NULL."""
    await store.upsert_preferences(
        user_id=USER, contrast_mode="standard", locale="en", theme="dark",
        theme_preset="slate", user_bubble_tone="mint", updated_at=1.0,
    )
    prefs = await store.get_preferences(user_id=USER)
    assert (prefs.chat_model_tier, prefs.agent_model_tier) == ("", "")


@pytest.mark.asyncio
async def test_distinct_users_independent(store) -> None:
    await store.upsert_preferences(
        user_id=USER_A, contrast_mode="high", locale="de", theme="dark",
        theme_preset="slate", user_bubble_tone="sky", updated_at=1.0,
    )
    await store.upsert_preferences(
        user_id=USER_B, contrast_mode="standard", locale="en", theme="light",
        theme_preset="standard", user_bubble_tone="gray", updated_at=1.0,
    )
    assert (await store.get_preferences(user_id=USER_A)).theme == "dark"
    assert (await store.get_preferences(user_id=USER_B)).theme == "light"


@pytest.mark.asyncio
async def test_db_check_rejects_out_of_domain_theme(store) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await store.upsert_preferences(
            user_id=USER, contrast_mode="standard", locale="en", theme="neon",
            theme_preset="standard", user_bubble_tone="gray", updated_at=1.0,
        )


@pytest.mark.asyncio
async def test_db_check_rejects_out_of_domain_user_bubble_tone(store) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        await store.upsert_preferences(
            user_id=USER, contrast_mode="standard", locale="en", theme="system",
            theme_preset="standard", user_bubble_tone="rainbow", updated_at=1.0,
        )
