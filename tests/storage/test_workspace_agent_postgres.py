"""Workspace agent against the durable backend (gated suite).

Proves the pieces the memory tier cannot: the PostgresSaver checkpoint
roundtrip across park/resume, the decide-in-transaction resume driven
through the REAL control service, and the loud checkpoint-wipe failure
(control rows intact, assignment restartable as a new run).
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

import inqtrix.research.web_research as web_research_module
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.agents.test_workspace_agent import (
    FakeSearch,
    RESEARCH_PLAN,
    ScriptedLLM,
    fake_child_graph,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


async def _wipe(tables: list[str]) -> None:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            async with session.begin():
                for table in tables:
                    await session.execute(
                        text(f'DELETE FROM "{table}"')
                    )
    finally:
        await engine.dispose()


@pytest.fixture()
def pg_agent_client(monkeypatch):
    asyncio.run(
        _wipe(
            [
                "run_events",
                "runs",
                "agent_sessions",
                "agent_session_groups",
            ]
        )
    )
    monkeypatch.setattr(
        web_research_module, "run_web_graph", fake_child_graph
    )
    app = FastAPI()
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(
            backend="postgres", database_url=TEST_DATABASE_URL
        ),
    )
    scripted = ScriptedLLM()
    container = register_routes(
        router,
        providers=SimpleNamespace(llm=scripted, search=FakeSearch()),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.llm = scripted  # type: ignore[attr-defined]
    client.container = container  # type: ignore[attr-defined]
    yield client
    container.run_store.close()


def _wait(client: TestClient, run_id: str, statuses: set[str], timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.05)
    pytest.fail(f"run {run_id} never reached {statuses}")


def test_park_resume_checkpoint_roundtrip_on_postgres(pg_agent_client):
    """Full arc on the durable backend: PostgresSaver checkpoint, park,
    decide-in-transaction resume, memo artifact, child run."""
    client = pg_agent_client
    # Keep the child-producing plan explicit. The shared ScriptedLLM default
    # intentionally exercises the lightweight web_instant path, while this
    # durable contract needs a real child run across the park/resume boundary.
    client.llm.scripts["ExecutionPlanModel"] = RESEARCH_PLAN
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle eine Marktanalyse.",
                "mode": "workspace_agent",
                "session_id": "sess-pg",
                "tool_directives": ["web_research"],
            },
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        _wait(client, run_id, {"waiting_for_approval"})

        # Checkpoint rows exist for this thread (durable resumability).
        async def _count() -> int:
            engine = build_engine(TEST_DATABASE_URL)
            try:
                factory = build_session_factory(engine)
                async with factory() as session:
                    return (
                        await session.execute(
                            text(
                                "SELECT count(*) FROM checkpoints "
                                "WHERE thread_id = :tid"
                            ),
                            {"tid": run_id},
                        )
                    ).scalar_one()
            finally:
                await engine.dispose()

        assert asyncio.run(_count()) > 0

        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        _wait(client, run_id, {"completed"})

        children = client.get(f"/v1/runs/{run_id}/children").json()["data"]
        assert len(children) == 1
        artifacts = client.get(f"/v1/runs/{run_id}/artifacts").json()["data"]
        memo = [a for a in artifacts if a["kind"] == "memo"]
        assert memo and memo[0]["status"] == "ready"
        # Terminal cleanup removed the thread's checkpoints.
        assert asyncio.run(_count()) == 0


def test_checkpoint_wipe_fails_loudly_and_stays_restartable(pg_agent_client):
    client = pg_agent_client
    with client:
        response = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle eine Marktanalyse.",
                "mode": "workspace_agent",
            },
        )
        run_id = response.json()["run_id"]
        _wait(client, run_id, {"waiting_for_approval"})

        # Simulate an operator wiping the library-owned checkpoint
        # tables while the run waits.
        asyncio.run(
            _wipe(["checkpoint_writes", "checkpoint_blobs", "checkpoints"])
        )

        approvals = client.get(f"/v1/runs/{run_id}/approvals").json()["data"]
        client.post(
            f"/v1/runs/{run_id}/approvals/{approvals[0]['approval_id']}",
            json={"decision": "approve"},
        )
        summary = _wait(client, run_id, {"failed"})
        assert "checkpoint for this agent run was lost" in summary["error"]["message"]

        # Control rows survived and a NEW run starts cleanly.
        plan = client.get(f"/v1/runs/{run_id}/plan").json()
        assert plan["version"] == 1
        fresh = client.post(
            "/v1/runs",
            json={
                "question": "Erstelle eine Marktanalyse.",
                "mode": "workspace_agent",
                "autonomy": "autonomous",
            },
        )
        assert fresh.status_code == 202
        _wait(client, fresh.json()["run_id"], {"completed"})
