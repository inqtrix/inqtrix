"""Postgres integration tests for the skill repository.

Gated like the sibling suites: a disposable database via
``INQTRIX_TEST_DATABASE_URL``, operations under the restricted app
role. Pins canonical-owner integrity, the CRUD roundtrip over the full
field set (JSONB points and tool lists included), non-disclosing
authorization failures, and the compare-and-set update distinction.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.content.skills import (
    SkillConflict,
    SkillNotFound,
    SkillRecord,
    new_skill_id,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.skills_postgres import PostgresSkillRepository
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_ID = canonical_user_id("skill-owner")
STRANGER_ID = canonical_user_id("skill-stranger")
MISSING_OWNER_ID = canonical_user_id("skill-missing-owner")


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
            await session.execute(text("DELETE FROM skill_templates"))
            await ensure_canonical_users(
                session,
                (OWNER_ID, STRANGER_ID),
            )
    yield PostgresSkillRepository(session_factory=factory, app_role=APP_ROLE)
    await engine.dispose()


def record(**overrides) -> SkillRecord:
    now = time.time()
    base = dict(
        id=new_skill_id(),
        tenant_id="default",
        owner_user_id=OWNER_ID,
        label="sprechzettel",
        title="Sprechzettel",
        description="Kompakter Sprechzettel.",
        when_to_use="Fuer Termine.",
        instructions_markdown="Sprechzettel fuer {{anlass}}.",
        clarification_points=(
            {
                "id": "p1",
                "name": "anlass",
                "question": "Welcher Anlass?",
                "options": [
                    {"id": "p1_o1", "label": "Vorstand", "description": ""}
                ],
                "required": True,
                "default_assumption": "Intern",
            },
        ),
        deliverable="talking_points",
        allowed_tools=("search_project_knowledge",),
        requires_plan="never",
        invocation="model_allowed",
        argument_hint="Anlass",
        model_tier="mid",
        effort="low",
        include_in_autocomplete=True,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SkillRecord(**base)


@pytest.mark.asyncio
async def test_crud_roundtrip_full_field_set(repository):
    created = await repository.create(record())
    fetched = await repository.get(created.id, tenant_id="default")
    assert fetched == created

    listed = await repository.list_for_tenant(tenant_id="default")
    assert [item.id for item in listed] == [created.id]

    updated = await repository.update(
        replace(created, title="Neuer Titel"),
        expected_revision=created.revision,
        actor_user_id=OWNER_ID,
    )
    assert updated.title == "Neuer Titel"
    assert updated.updated_at > created.updated_at
    assert updated.revision == created.revision + 1
    assert updated.clarification_points == created.clarification_points

    await repository.delete(
        created.id,
        tenant_id="default",
        actor_user_id=OWNER_ID,
    )
    with pytest.raises(SkillNotFound):
        await repository.get(created.id, tenant_id="default")


@pytest.mark.asyncio
async def test_absence_and_foreign_tenant_raise_alike(repository):
    created = await repository.create(record())
    with pytest.raises(SkillNotFound):
        await repository.get("sk_missing", tenant_id="default")
    with pytest.raises(SkillNotFound):
        await repository.get(created.id, tenant_id="tenant-x")
    with pytest.raises(SkillNotFound):
        await repository.delete(
            "sk_missing",
            tenant_id="default",
            actor_user_id=OWNER_ID,
        )


@pytest.mark.asyncio
async def test_share_facts_read_owner_and_title_of_the_tenant_only(repository):
    from inqtrix.auth.shares import ShareFacts

    owned = await repository.create(record(title="Owned"))

    assert await repository.share_facts(
        [owned.id, "sk_missing"], tenant_id="default"
    ) == {owned.id: ShareFacts(OWNER_ID, "Owned")}
    assert await repository.share_facts([owned.id], tenant_id="tenant-x") == {}
    assert await repository.share_facts([], tenant_id="default") == {}


@pytest.mark.asyncio
async def test_create_requires_a_live_canonical_owner(repository):
    candidate = record(owner_user_id=MISSING_OWNER_ID)

    with pytest.raises(SkillNotFound):
        await repository.create(candidate)
    with pytest.raises(SkillNotFound):
        await repository.get(candidate.id, tenant_id="default")


@pytest.mark.asyncio
async def test_ownerless_record_roundtrips_in_unscoped_mode(repository):
    created = await repository.create(record(owner_user_id=None))

    assert (
        await repository.get(created.id, tenant_id="default")
    ).owner_user_id is None


@pytest.mark.asyncio
async def test_actor_cannot_mutate_or_delete_another_owners_skill(repository):
    created = await repository.create(record(title="owner value"))

    with pytest.raises(SkillNotFound):
        await repository.update(
            replace(created, title="stranger value"),
            expected_revision=created.revision,
            actor_user_id=STRANGER_ID,
        )
    with pytest.raises(SkillNotFound):
        await repository.delete(
            created.id,
            tenant_id="default",
            actor_user_id=STRANGER_ID,
        )

    stored = await repository.get(created.id, tenant_id="default")
    assert stored.title == "owner value"
    assert stored.revision == created.revision


@pytest.mark.asyncio
async def test_matching_precondition_updates_stale_one_conflicts(repository):
    created = await repository.create(record(title="v0"))
    advanced = await repository.update(
        replace(created, title="v1"),
        expected_revision=created.revision,
        actor_user_id=OWNER_ID,
    )
    assert advanced.title == "v1"

    with pytest.raises(SkillConflict) as exc_info:
        await repository.update(
            replace(created, title="stale"),
            expected_revision=created.revision,
            actor_user_id=OWNER_ID,
        )
    assert exc_info.value.current_revision == advanced.revision
    with pytest.raises(SkillNotFound):
        await repository.update(
            record(id="sk_missing", title="ghost"),
            expected_revision=created.revision,
            actor_user_id=OWNER_ID,
        )
