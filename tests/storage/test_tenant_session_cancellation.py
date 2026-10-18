"""Cancellation contracts for the tenant-scoped database transaction."""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from anyio import CancelScope, sleep
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import text

from inqtrix.server.database_gate import install_database_contract_gate
from inqtrix.settings import Settings
from inqtrix.storage.db import (
    build_engine,
    build_session_factory,
    tenant_session,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> "_Transaction":
        self._events.append("transaction_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        del exc, traceback
        await sleep(0)
        name = "none" if exc_type is None else exc_type.__name__
        self._events.append(f"transaction_exit:{name}")
        return False


class _Session:
    def __init__(
        self,
        events: list[str],
        *,
        cancel_scope: CancelScope | None = None,
        cancel_on_execute: int | None = None,
    ) -> None:
        self._cancel_on_execute = cancel_on_execute
        self._cancel_scope = cancel_scope
        self._events = events
        self._execute_count = 0
        self._transaction = _Transaction(events)

    async def __aenter__(self) -> "_Session":
        self._events.append("session_enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        await self.close()

    def begin(self) -> _Transaction:
        return self._transaction

    async def execute(self, statement: object, parameters: object = None) -> object:
        del statement, parameters
        self._execute_count += 1
        self._events.append(f"execute:{self._execute_count}")
        if (
            self._cancel_scope is not None
            and self._cancel_on_execute == self._execute_count
        ):
            self._cancel_scope.cancel()
            await sleep(0)
        return object()

    async def close(self) -> None:
        await sleep(0)
        self._events.append("session_close")


class _SessionFactory:
    kw: dict[str, object] = {}

    def __init__(self, session: _Session) -> None:
        self._session = session

    def __call__(self) -> _Session:
        return self._session


@pytest.mark.asyncio
async def test_tenant_session_finishes_cleanup_when_initialization_is_cancelled() -> None:
    events: list[str] = []

    with CancelScope() as scope:
        session = _Session(
            events,
            cancel_scope=scope,
            cancel_on_execute=1,
        )
        async with tenant_session(
            _SessionFactory(session),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            app_role="inqtrix_app",
        ):
            pytest.fail("cancelled tenant initialization reached the context body")

    assert scope.cancelled_caught is True
    assert events == [
        "session_enter",
        "transaction_enter",
        "execute:1",
        "transaction_exit:CancelledError",
        "session_close",
    ]


@pytest.mark.asyncio
async def test_tenant_session_finishes_cleanup_when_context_body_is_cancelled() -> None:
    events: list[str] = []

    with CancelScope() as scope:
        session = _Session(events)
        async with tenant_session(
            _SessionFactory(session),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            app_role="inqtrix_app",
        ):
            events.append("body")
            scope.cancel()
            await sleep(0)

    assert scope.cancelled_caught is True
    assert events == [
        "session_enter",
        "transaction_enter",
        "execute:1",
        "execute:2",
        "body",
        "transaction_exit:CancelledError",
        "session_close",
    ]


@pytest.mark.asyncio
async def test_tenant_session_finishes_cleanup_when_task_is_cancelled() -> None:
    events: list[str] = []
    entered_body = asyncio.Event()
    session = _Session(events)

    async def use_session() -> None:
        async with tenant_session(
            _SessionFactory(session),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            app_role="inqtrix_app",
        ):
            events.append("body")
            entered_body.set()
            await asyncio.Future()

    task = asyncio.create_task(use_session())
    await entered_body.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert events == [
        "session_enter",
        "transaction_enter",
        "execute:1",
        "execute:2",
        "body",
        "transaction_exit:CancelledError",
        "session_close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_error", "expected_exit"),
    [
        (None, "transaction_exit:none"),
        (RuntimeError("body failed"), "transaction_exit:RuntimeError"),
    ],
)
async def test_tenant_session_keeps_commit_and_rollback_exit_contracts(
    body_error: RuntimeError | None,
    expected_exit: str,
) -> None:
    events: list[str] = []
    session = _Session(events)

    async def use_session() -> None:
        async with tenant_session(
            _SessionFactory(session),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            app_role="inqtrix_app",
        ):
            events.append("body")
            if body_error is not None:
                raise body_error

    if body_error is None:
        await use_session()
    else:
        with pytest.raises(RuntimeError, match="body failed"):
            await use_session()

    assert events == [
        "session_enter",
        "transaction_enter",
        "execute:1",
        "execute:2",
        "body",
        expected_exit,
        "session_close",
    ]


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_real_tenant_session_returns_connection_after_query_cancellation() -> (
    None
):
    engine = build_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    session_factory = build_session_factory(engine)
    query_started = asyncio.Event()

    async def run_query() -> None:
        async with tenant_session(
            session_factory,
            tenant_id="tenant-a",
            app_role="",
        ) as session:
            query_started.set()
            await session.execute(text("SELECT pg_sleep(30)"))

    try:
        task = asyncio.create_task(run_query())
        await query_started.wait()
        await _wait_for_checked_out_connection(engine)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await _wait_for_empty_pool(engine)
        async with tenant_session(
            session_factory,
            tenant_id="tenant-a",
            app_role="",
        ) as session:
            assert await session.scalar(text("SELECT 1")) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_database_gate_sse_disconnect_returns_tenant_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = build_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    session_factory = build_session_factory(engine)
    query_started = asyncio.Event()
    disconnect = asyncio.Event()
    request_sent = False

    app = FastAPI()
    settings = Settings()
    settings.storage.backend = "postgres"  # type: ignore[assignment]
    install_database_contract_gate(
        app,
        container=SimpleNamespace(settings=settings),
    )
    app.state.database_contract_ready = True

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def body():
            async with tenant_session(
                session_factory,
                tenant_id="tenant-a",
                app_role="",
            ) as session:
                query_started.set()
                await session.execute(text("SELECT pg_sleep(30)"))
                yield b"unreachable"

        return StreamingResponse(body())

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 41000),
        "server": ("testserver", 80),
        "root_path": "",
    }

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        del message

    try:
        with caplog.at_level(
            logging.ERROR,
            logger="sqlalchemy.pool.impl.AsyncAdaptedQueuePool",
        ):
            request_task = asyncio.create_task(app(scope, receive, send))
            await query_started.wait()
            await _wait_for_checked_out_connection(engine)
            disconnect.set()
            await asyncio.wait_for(request_task, timeout=5)

        await _wait_for_empty_pool(engine)
        async with tenant_session(
            session_factory,
            tenant_id="tenant-a",
            app_role="",
        ) as session:
            assert await session.scalar(text("SELECT 1")) == 1
        assert not any(
            record.getMessage().startswith("Exception terminating connection")
            for record in caplog.records
        )
    finally:
        await engine.dispose()


async def _wait_for_checked_out_connection(engine: object) -> None:
    await _wait_for_pool_count(engine, expected=1)


async def _wait_for_empty_pool(engine: object) -> None:
    await _wait_for_pool_count(engine, expected=0)


async def _wait_for_pool_count(engine: object, *, expected: int) -> None:
    pool = engine.pool  # type: ignore[attr-defined]
    for _ in range(100):
        if pool.checkedout() == expected:
            return
        await asyncio.sleep(0.01)
    assert pool.checkedout() == expected
