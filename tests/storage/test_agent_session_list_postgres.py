"""The Postgres agent-session list carries each session's settings body."""

import os
import uuid

import pytest

from inqtrix.project.agent_sessions_postgres import PostgresAgentSessionStore
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import canonical_user_id, ensure_canonical_users

pytestmark = pytest.mark.postgres
URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
OWNER = canonical_user_id("agent-session-list-owner")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    # Outside the test's event loop: run_migrations drives its own.
    if URL:
        run_migrations(URL)
    yield


@pytest.mark.asyncio
async def test_the_postgres_session_list_carries_each_settings_body():
    """Same contract as the memory store: a list row holds ``items_json``, so a
    client compares its sessions against the real row, not default settings."""
    engine = build_engine(URL)
    factory = build_session_factory(engine)
    async with factory() as session, session.begin():
        await ensure_canonical_users(session, (OWNER,))
    store = PostgresAgentSessionStore(engine=engine, app_role="inqtrix_app")
    workspace = f"workspace-{uuid.uuid4()}"
    session_id = f"as-list-{uuid.uuid4()}"
    body = '{"source_policy": {"web": "disabled", "knowledge": "available"}}'
    try:
        await store.upsert_session(
            id=session_id, title="Liste", items_json=body, group_id=None,
            created_at=1.0, updated_at=2.0, created_by_user_id=OWNER,
            workspace_id=workspace,
        )
        listed = await store.list_sessions(created_by_user_id=OWNER, workspace_id=workspace)
        assert [(row.id, row.items_json) for row in listed] == [(session_id, body)]
    finally:
        async with factory() as session, session.begin():
            await session.execute(agent_sessions.delete().where(agent_sessions.c.id == session_id))
        await store.aclose()
