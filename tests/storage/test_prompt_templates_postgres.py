"""Postgres integration tests for the prompt-template repository.

Gated like the sibling suites: a disposable database via
``INQTRIX_TEST_DATABASE_URL`` and operations under the restricted app
role. The suite pins canonical-owner integrity, CRUD round-trips,
non-disclosing absence semantics, and guarded revision updates.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.content.prompt_templates import (
    PromptTemplateConflict,
    PromptTemplateNotFound,
    PromptTemplateRecord,
    new_template_id,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.prompt_templates_postgres import (
    PostgresPromptTemplateRepository,
)
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_ID = canonical_user_id("prompt-template-owner")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def repository():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DELETE FROM prompt_templates"))
            await ensure_canonical_users(session, (OWNER_ID,))
    yield PostgresPromptTemplateRepository(
        session_factory=factory, app_role=APP_ROLE
    )
    await engine.dispose()


def record(**overrides) -> PromptTemplateRecord:
    now = time.time()
    base = dict(
        id=new_template_id(),
        tenant_id="default",
        owner_user_id=OWNER_ID,
        title="Executive Briefing",
        label="briefing",
        category="instruction",
        content_markdown="Fasse die Lage zusammen.",
        visibility={"chat": True, "editor": False},
        include_in_autocomplete=True,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return PromptTemplateRecord(**base)


@pytest.mark.asyncio
async def test_crud_roundtrip(repository):
    created = await repository.create(record())
    fetched = await repository.get(created.id, tenant_id="default")
    assert fetched == created

    listed = await repository.list_for_tenant(tenant_id="default")
    assert [item.id for item in listed] == [created.id]

    updated = await repository.update(
        PromptTemplateRecord(
            **{
                **created.__dict__,
                "title": "Neuer Titel",
            }
        ),
        expected_revision=created.revision,
        actor_user_id=OWNER_ID,
    )
    assert updated.title == "Neuer Titel"
    # The repository stamps the LWW anchor itself.
    assert updated.updated_at > created.updated_at
    assert updated.revision == created.revision + 1

    await repository.delete(
        created.id, tenant_id="default", actor_user_id=OWNER_ID
    )
    with pytest.raises(PromptTemplateNotFound):
        await repository.get(created.id, tenant_id="default")


@pytest.mark.asyncio
async def test_absence_and_foreign_tenant_raise_alike(repository):
    created = await repository.create(record())
    with pytest.raises(PromptTemplateNotFound):
        await repository.get("pt_missing", tenant_id="default")
    with pytest.raises(PromptTemplateNotFound):
        await repository.get(created.id, tenant_id="tenant-x")
    with pytest.raises(PromptTemplateNotFound):
        await repository.delete(
            "pt_missing", tenant_id="default", actor_user_id=OWNER_ID
        )


@pytest.mark.asyncio
async def test_share_facts_read_owner_and_title_of_the_tenant_only(repository):
    from inqtrix.auth.shares import ShareFacts

    owned = await repository.create(record(title="Owned"))
    ownerless = await repository.create(
        record(owner_user_id=None, title="Ownerless", label="ownerless")
    )

    assert await repository.share_facts(
        [owned.id, ownerless.id, "pt_missing"], tenant_id="default"
    ) == {
        owned.id: ShareFacts(OWNER_ID, "Owned"),
        ownerless.id: ShareFacts(None, "Ownerless"),
    }
    assert await repository.share_facts([owned.id], tenant_id="tenant-x") == {}
    assert await repository.share_facts([], tenant_id="default") == {}


@pytest.mark.asyncio
async def test_ownerless_record_roundtrips(repository):
    created = await repository.create(record(owner_user_id=None))
    fetched = await repository.get(created.id, tenant_id="default")
    assert fetched.owner_user_id is None


@pytest.mark.asyncio
async def test_matching_precondition_updates_stale_one_conflicts(repository):
    """The guarded UPDATE is atomic compare-and-set, with a precise
    404-vs-409 distinction."""
    created = await repository.create(record(title="v0"))

    # Matching revision: the write lands and advances both fields.
    fresh = await repository.update(
        replace(created, title="v1"),
        expected_revision=created.revision,
        actor_user_id=OWNER_ID,
    )
    assert fresh.title == "v1"
    assert fresh.updated_at > created.updated_at

    # Stale revision: conflict, no overwrite.
    with pytest.raises(PromptTemplateConflict) as exc_info:
        await repository.update(
            replace(created, title="v2"),
            expected_revision=created.revision,
            actor_user_id=OWNER_ID,
        )
    assert exc_info.value.current_revision == fresh.revision
    assert (
        await repository.get(created.id, tenant_id="default")
    ).title == "v1"

@pytest.mark.asyncio
async def test_precondition_on_missing_row_is_not_found_not_conflict(repository):
    """A precondition against a vanished row is 404, never a phantom 409."""
    ghost = record(id="pt_ghost")
    with pytest.raises(PromptTemplateNotFound):
        await repository.update(
            ghost,
            expected_revision=1,
            actor_user_id=OWNER_ID,
        )


@pytest.mark.asyncio
async def test_seed_claims_once_and_respects_a_grown_library(repository):
    """The marker+insert transaction seeds at most once per user."""
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("DELETE FROM prompt_template_seed_markers")
            )
    await engine.dispose()

    first = [record(), record(title="Zweite", label="zweite")]
    assert await repository.seed_default_templates(
        first, tenant_id="default", user_id=OWNER_ID
    ) is True
    listed = await repository.list_for_tenant(tenant_id="default")
    assert {item.id for item in listed} == {item.id for item in first}

    # Second call: marker already claimed — nothing inserted.
    assert await repository.seed_default_templates(
        [record()], tenant_id="default", user_id=OWNER_ID
    ) is False
    assert len(await repository.list_for_tenant(tenant_id="default")) == 2

    # Deleting a seeded row must not reopen the claim.
    await repository.delete(
        first[0].id, tenant_id="default", actor_user_id=OWNER_ID
    )
    assert await repository.seed_default_templates(
        [record()], tenant_id="default", user_id=OWNER_ID
    ) is False
    assert len(await repository.list_for_tenant(tenant_id="default")) == 1


@pytest.mark.asyncio
async def test_seed_marks_without_rows_when_the_user_already_owns(repository):
    """A library grown before the first listing is never injected into."""
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text("DELETE FROM prompt_template_seed_markers")
            )
    await engine.dispose()

    existing = await repository.create(record(title="Eigen", label="eigen"))
    assert await repository.seed_default_templates(
        [record()], tenant_id="default", user_id=OWNER_ID
    ) is False
    listed = await repository.list_for_tenant(tenant_id="default")
    assert [item.id for item in listed] == [existing.id]
