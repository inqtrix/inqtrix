"""Agent runs against the durable backend (gated suite).

Proves what the memory tier cannot: the PostgresSaver checkpoint roundtrip
across a park and its resume, driven through the real control service, and
the loud failure of a run whose checkpoint was wiped while it waited, after
which the same assignment starts cleanly as a new run.
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("langchain")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from inqtrix import aio
from inqtrix.server.routes import create_router, register_routes
from inqtrix.settings import (
    AgentPlatformSettings,
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from tests.agents.test_kernel_read_tools_policy import (
    RecordingSearch,
    ScriptedToolLLM,
    _pending_tool_approval,
    _text_turn,
    _tool_turn,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

QUESTION = "Was ist neu beim EU AI Act?"


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
                    await session.execute(text(f'DELETE FROM "{table}"'))
    finally:
        await engine.dispose()


async def _checkpoint_rows(run_id: str) -> int:
    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            return (
                await session.execute(
                    text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"),
                    {"tid": run_id},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


def _client(turns: list[Any]) -> TestClient:
    asyncio.run(_wipe(["run_events", "runs", "agent_sessions", "agent_session_groups"]))

    @asynccontextmanager
    async def on_a_process_loop(_app: FastAPI):
        loop = asyncio.get_running_loop()
        aio.configure_loop(
            loop, blocking_workers=8, provider_workers=8, legacy_segment_workers=16, watch=False
        )
        try:
            yield
        finally:
            await container.run_store.aclose()
            await container.database_engine.dispose()
            # The checkpointer owns a pool on its own driver that no engine
            # disposal reaches; left open, it holds a session later tests see.
            if container.agent_checkpointer is not None:
                container.agent_checkpointer.close()
            aio.close_loop(loop)

    app = FastAPI(lifespan=on_a_process_loop)
    router = create_router()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=ServerSettings(),
        storage=StorageSettings(backend="postgres", database_url=TEST_DATABASE_URL),
    )
    settings.agent_platform = AgentPlatformSettings(INQTRIX_AGENT_KERNEL_ENABLED=True)
    llm = ScriptedToolLLM(turns)
    container = register_routes(
        router,
        providers=SimpleNamespace(llm=llm, search=RecordingSearch()),
        strategies=SimpleNamespace(),
        settings=settings,
        semaphore_factory=lambda: asyncio.Semaphore(2),
    )
    app.include_router(router)
    client = TestClient(app)
    client.container = container  # type: ignore[attr-defined]
    return client


def _wait(client: TestClient, run_id: str, statuses: set[str], timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary["status"] in statuses:
            return summary
        time.sleep(0.05)
    pytest.fail(f"run {run_id} never reached {statuses}")


def _submit(client: TestClient, autonomy: str) -> str:
    response = client.post(
        "/v1/runs", json={"question": QUESTION, "mode": "agent_kernel", "autonomy": autonomy}
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_park_resume_checkpoint_roundtrip_on_postgres():
    """Park at a gated search, resume from the stored checkpoint after the
    approval, and leave no thread behind once the run completed."""
    client = _client([
        _tool_turn("call_search", "web_instant", {"query": "EU AI Act Neuigkeiten"}),
        _text_turn("Antwort aus der Suche."),
    ])
    with client:
        run_id = _submit(client, "balanced")
        _wait(client, run_id, {"waiting_for_approval"})
        assert asyncio.run(_checkpoint_rows(run_id)) > 0

        approval = _pending_tool_approval(client, run_id)
        decided = client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
            json={"decision": "approve"},
        )
        assert decided.status_code == 200, decided.text
        _wait(client, run_id, {"completed"})

    assert asyncio.run(_checkpoint_rows(run_id)) == 0


def test_checkpoint_wipe_fails_loudly_and_stays_restartable():
    client = _client([
        _tool_turn("call_search", "web_instant", {"query": "EU AI Act Neuigkeiten"}),
        _text_turn("Antwort ohne Suche."),
    ])
    with client:
        run_id = _submit(client, "balanced")
        _wait(client, run_id, {"waiting_for_approval"})

        # An operator wipes the library-owned checkpoint tables while the run waits.
        asyncio.run(_wipe(["checkpoint_writes", "checkpoint_blobs", "checkpoints"]))

        approval = _pending_tool_approval(client, run_id)
        client.post(
            f"/v1/runs/{run_id}/approvals/{approval['approval_id']}",
            json={"decision": "approve"},
        )
        summary = _wait(client, run_id, {"failed"})
        assert "checkpoint for this agent run was lost" in summary["error"]["message"]

        fresh = _submit(client, "autonomous")
        _wait(client, fresh, {"completed"})
