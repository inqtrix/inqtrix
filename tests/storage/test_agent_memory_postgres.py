"""Agent-memory persistence under canonical-user and tenant RLS contracts.

The restricted application role must be able to round-trip candidates and
feedback for an active canonical user while FORCE ROW LEVEL SECURITY scopes
every operation to the transaction-local tenant GUC. Live Postgres only; the
suite is skipped offline.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from inqtrix.agents.memory_ports import AgentFeedbackRecord, AgentMemoryCandidate
from inqtrix.storage.agent_memory_postgres import (
    PostgresAgentFeedbackStore,
    PostgresAgentMemoryCandidateStore,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_ID = canonical_user_id("agent-memory-user")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def wiped():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user to clean RLS-forced tables."
                )
            await session.execute(text("DELETE FROM agent_memory_candidates"))
            await session.execute(text("DELETE FROM agent_feedback"))
            await ensure_canonical_users(session, (USER_ID,))
    await engine.dispose()
    yield
    cleanup_engine = build_engine(TEST_DATABASE_URL)
    cleanup_factory = build_session_factory(cleanup_engine)
    try:
        async with cleanup_factory() as session:
            async with session.begin():
                await session.execute(text("DELETE FROM agent_memory_candidates"))
                await session.execute(text("DELETE FROM agent_feedback"))
    finally:
        await cleanup_engine.dispose()


@pytest_asyncio.fixture()
async def candidate_store(wiped):
    store = PostgresAgentMemoryCandidateStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    yield store
    await store.aclose()


@pytest_asyncio.fixture()
async def feedback_store(wiped):
    store = PostgresAgentFeedbackStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    yield store
    await store.aclose()


@pytest.mark.asyncio
async def test_memory_candidate_round_trips_under_the_app_tenant_guc(
    candidate_store,
):
    """Candidates remain visible under the same canonical user and tenant."""
    candidate = AgentMemoryCandidate(
        candidate_id="cand-1",
        tenant_id="default",
        user_id=USER_ID,
        scope="user",
        category="preference",
        content="prefers concise answers",
        reason="observed across runs",
        confidence=0.8,
        source_run_id="run-1",
    )

    created = await candidate_store.create_candidate(candidate)
    assert created.candidate_id == "cand-1"

    listed = await candidate_store.list_candidates(
        tenant_id="default", user_id=USER_ID
    )
    assert [c.candidate_id for c in listed] == ["cand-1"]
    assert listed[0].content == "prefers concise answers"


@pytest.mark.asyncio
async def test_feedback_round_trips_under_the_app_tenant_guc(feedback_store):
    """Feedback remains visible under the same canonical user and tenant."""
    record = AgentFeedbackRecord(
        feedback_id="fb-1",
        tenant_id="default",
        user_id=USER_ID,
        run_id="run-1",
        feedback="positive",
        reason="clear answer",
    )

    created = await feedback_store.create_feedback(record)
    assert created.feedback_id == "fb-1"

    listed = await feedback_store.list_feedback(
        tenant_id="default", user_id=USER_ID
    )
    assert [f.feedback_id for f in listed] == ["fb-1"]
