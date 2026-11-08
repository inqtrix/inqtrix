"""A process keeps one database engine, and everything on Postgres shares it.

`create_async_engine` opens nothing until the first statement, so the real
composition runs here with a dummy URL.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from inqtrix.auth.api_key import build_auth_provider
from inqtrix.providers.base import ProviderContext
from inqtrix.server import create_app
from inqtrix.server.app import _vector_write_slots as _vector_write_slots_of_api
from inqtrix.server.container import build_chat_store
from inqtrix.settings import (
    AuthSettings,
    CollaborationSettings,
    EditorGuestLinkSettings,
    KnowledgeSettings,
    QueueSettings,
    QuotaSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)
from inqtrix.storage import db as db_module
from inqtrix.worker.__main__ import (
    _compose_worker,
    _DatabaseClaimGuard,
    _vector_write_slots,
)
from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM

_URL = "postgresql+asyncpg://u:p@127.0.0.1:1/db"


def _everything_on() -> Settings:
    return Settings(
        server=ServerSettings(public_base_url="https://inqtrix.example"),
        storage=StorageSettings(backend="postgres", database_url=_URL),
        auth=AuthSettings(mode="local", session_secret="s" * 40, pat_pepper="p" * 40),
        queue=QueueSettings(valkey_url="redis://127.0.0.1:1/0"),
        quota=QuotaSettings(enabled=True),
        knowledge=KnowledgeSettings(
            enabled=True,
            embedding_model="text-embedding-3-small",
            embedding_api_key="k",
            embedding_base_url="http://127.0.0.1:1",
        ),
        collaboration=CollaborationSettings(
            enabled=True,
            http_url="http://collaboration:1234",
            ws_url="ws://collaboration:1234/collaboration",
            secret="c" * 40,
        ),
        editor_guest_links=EditorGuestLinkSettings(
            enabled=True, token_hmac_secret="g" * 40
        ),
    )


def _providers() -> ProviderContext:
    return ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch())


@pytest.fixture
def built(monkeypatch) -> list[Any]:
    """Every engine, whatever its pool: a count of pools alone misses a NullPool."""
    engines: list[Any] = []
    real = db_module.create_async_engine

    def recording(*args: Any, **kwargs: Any) -> Any:
        engine = real(*args, **kwargs)
        engines.append(engine)
        return engine

    monkeypatch.setattr(db_module, "create_async_engine", recording)
    return engines


def _bound_engine(store: Any) -> Any:
    return store._session_factory.kw["bind"]


def test_the_api_builds_one_engine_for_everything(built) -> None:
    settings = _everything_on()

    app = create_app(settings=settings, providers=_providers())

    assert len(built) == 1
    engine = built[0]
    assert app.state.container.database_engine is engine
    assert not isinstance(engine.pool, NullPool)
    assert engine.pool.size() == settings.storage.pool_size
    assert engine.pool._max_overflow == settings.storage.pool_max_overflow


def test_the_worker_builds_one_engine_for_everything(built) -> None:
    settings = _everything_on()
    guard = _DatabaseClaimGuard(
        database_url=settings.storage.database_url,
        app_role=settings.storage.app_role,
        login_policy=settings.storage.runtime_login_policy,
    )

    worker = _compose_worker(settings, guard)

    assert len(built) == 1
    assert worker.database_engine is built[0]
    assert not isinstance(built[0].pool, NullPool)


def test_every_store_of_the_api_sits_on_the_engine(built) -> None:
    container = create_app(
        settings=_everything_on(), providers=_providers()
    ).state.container
    engine = container.database_engine

    for name, store in {
        "run": container.run_store,
        "indexing": container.indexing_service.job_store,
        "deletion": container.asset_deletion_service.operation_store,
        "upload": container.upload_operation_service.operations,
        "chat": container.chat_history_service.store,
        "editor": container.editor_persistence_service.store,
        "patch": container.editor_patch_service.store,
        "asset": container.asset_records_service.store,
        "vector index": container.vector_index_service.store,
        "preferences": container.account_preferences_service.store,
        "knowledge sessions": container.knowledge_sessions_service.store,
        "knowledge": container.knowledge_service.knowledge.store,
        "quota": container.quota_service._store,
        "users": container.auth_provider.users,
        "lookup": container.run_user_lookup,
        "members": container.permission_service._members,
    }.items():
        assert _bound_engine(store) is engine, name


def test_a_second_session_factory_is_not_a_second_engine(built) -> None:
    """Stores keep factories of their own; only the engine has to be one."""
    container = create_app(
        settings=_everything_on(), providers=_providers()
    ).state.container

    assert container.session_factory.kw["bind"] is container.database_engine
    assert len(built) == 1


def test_the_settings_keep_what_they_open() -> None:
    storage = StorageSettings()

    assert storage.pool_size == 15
    assert storage.pool_max_overflow == 0


def test_a_process_without_postgres_builds_none(built) -> None:
    settings = Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
        auth=AuthSettings(mode="none"),
    )

    container = create_app(settings=settings, providers=_providers()).state.container

    assert built == []
    assert container.database_engine is None


def test_postgres_without_a_url_refuses_to_start(built) -> None:
    settings = Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="postgres", database_url=""),
        auth=AuthSettings(mode="none"),
    )

    with pytest.raises(RuntimeError, match="INQTRIX_DATABASE_URL"):
        create_app(settings=settings, providers=_providers())
    assert built == []


class _RecordingEngine:
    url = make_url(_URL)

    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def dispose(self) -> None:
        self._order.append("engine")


class _RecordingStore:
    def __init__(self, order: list[str], *, fails: bool = False) -> None:
        self._order = order
        self._fails = fails

    async def aclose(self) -> None:
        self._order.append("store")
        if self._fails:
            raise RuntimeError("store failed to close")


class _RecordingLedger:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.store = None

    def start(self) -> None:
        pass

    async def aclose(self) -> None:
        self._order.append("ledger")


@pytest.fixture
def lifespan_of_a_process(monkeypatch):
    """An app whose engine, run store and usage ledger write down when they close."""
    order: list[str] = []
    app = create_app(
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
            auth=AuthSettings(mode="none"),
        ),
        providers=_providers(),
    )
    ledger = _RecordingLedger(order)
    monkeypatch.setattr("inqtrix.usage.recorder.active_usage_recorder", lambda: ledger)
    app.state.container = dataclasses.replace(
        app.state.container,
        database_engine=_RecordingEngine(order),
        run_store=_RecordingStore(order),
    )
    return app, order


def test_the_engine_goes_after_everything_that_used_it(lifespan_of_a_process) -> None:
    app, order = lifespan_of_a_process

    with TestClient(app):
        pass

    assert order == ["store", "ledger", "engine"]


def test_each_lifespan_disposes_once_and_uses_nothing_after_it(lifespan_of_a_process) -> None:
    app, order = lifespan_of_a_process

    with TestClient(app):
        pass
    with TestClient(app):
        pass

    assert order == ["store", "ledger", "engine"] * 2


def test_a_store_that_fails_to_close_takes_nothing_after_it_along(
    lifespan_of_a_process, caplog
) -> None:
    app, order = lifespan_of_a_process
    app.state.container = dataclasses.replace(
        app.state.container, run_store=_RecordingStore(order, fails=True)
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with TestClient(app):
            pass

    assert order == ["store", "ledger", "engine"]
    assert "Closing the job store failed at shutdown" in caplog.text


def test_a_composed_worker_carries_what_its_exit_needs(built) -> None:
    settings = _everything_on()
    guard = SimpleNamespace(check=lambda: None)

    worker = _compose_worker(settings, guard)

    assert worker.stores
    for store in worker.stores:
        assert _bound_engine(store) is worker.database_engine


def test_a_disposed_engine_opens_a_new_pool() -> None:
    """Why a lifespan can start again: only a closed job store stays closed."""

    async def dispose_twice() -> None:
        engine = db_module.build_engine(_URL)
        await engine.dispose()
        pool_before = engine.pool
        await engine.dispose()
        assert engine.pool is not pool_before

    asyncio.run(dispose_twice())


def test_a_provider_on_another_database_refuses_to_start() -> None:
    settings = _everything_on()
    other = _everything_on()
    other.storage = StorageSettings(
        backend="postgres", database_url="postgresql+asyncpg://u:p@127.0.0.1:1/other"
    )
    provider = build_auth_provider(other)

    with pytest.raises(RuntimeError, match="another database"):
        create_app(settings=settings, providers=_providers(), auth_provider=provider)


def test_a_provider_from_the_same_settings_brings_the_engine(built) -> None:
    settings = _everything_on()
    provider = build_auth_provider(settings)

    app = create_app(settings=settings, providers=_providers(), auth_provider=provider)

    assert len(built) == 1
    assert app.state.container.database_engine is provider.database_engine


def test_a_store_without_an_engine_fails_where_it_is_built() -> None:
    with pytest.raises(RuntimeError, match="engine of its process"):
        build_chat_store(_everything_on())


def test_the_worker_warns_only_about_the_slots_it_has(monkeypatch) -> None:
    settings = _everything_on()
    settings.knowledge.reindex_max_concurrent = 20
    settings.server.deletion_max_concurrent = 2
    settings.queue.worker_concurrency = 3
    guard = SimpleNamespace(check=lambda: None)
    worker = _compose_worker(settings, guard)

    assert _vector_write_slots(settings, worker.loops) == 3 + 2
    settings.knowledge.enabled = False
    assert _vector_write_slots(settings, worker.loops) == 0


def test_the_api_counts_vector_writes_only_when_it_runs_them() -> None:
    settings = _everything_on()
    settings.knowledge.reindex_max_concurrent = 4
    settings.server.deletion_max_concurrent = 2

    settings.queue.backend = "valkey"
    assert _vector_write_slots_of_api(settings) == 0
    settings.queue.backend = "memory"
    assert _vector_write_slots_of_api(settings) == 6
    settings.knowledge.enabled = False
    assert _vector_write_slots_of_api(settings) == 0
