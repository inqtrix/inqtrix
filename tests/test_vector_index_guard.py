"""Startup guard: the in-process vector index serves one process only.

``INQTRIX_VECTOR_BACKEND=memory`` keeps the knowledge vectors inside each
process. Behind a Valkey queue the workers index documents the API process
never sees, and across declared replicas each one finds only what it indexed
itself; searches miss the rest without an error, so the composition refuses
both at startup and names the remedy. Beside Postgres the documents outlive
the vectors, which are gone after every restart, so a single process starts
with a loud WARNING. One process without Postgres is the quiet
zero-infrastructure trial; Qdrant and a disabled knowledge engine are never
refused or warned about.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.postgres_store import PostgresKnowledgeStore
from inqtrix.server.container import build_knowledge_context
from inqtrix.settings import (
    KnowledgeSettings,
    QueueSettings,
    Settings,
    StorageSettings,
)
from inqtrix.storage import db as db_module

_URL = "postgresql+asyncpg://u:p@127.0.0.1:1/db"
_COMPOSER = "inqtrix.server.container"
# The guard's and the warning's own prose: a reword must be caught here.
_REFUSAL = "keeps the knowledge vectors inside each process and cannot be shared"
_REMEDY = "Set INQTRIX_VECTOR_BACKEND=qdrant"
_VOLATILE = (
    "CONFIG: INQTRIX_VECTOR_BACKEND=memory keeps the knowledge vectors in this "
    "process while documents and chunks live in Postgres."
)


def _settings(
    *,
    storage: str = "memory",
    queue: str = "memory",
    replicas: int = 1,
    vectors: str = "memory",
    knowledge: bool = True,
) -> Settings:
    return Settings(
        storage=StorageSettings(
            backend=storage,
            database_url=_URL if storage == "postgres" else "",
            replica_count=replicas,
        ),
        queue=QueueSettings(
            backend=queue,
            valkey_url="redis://127.0.0.1:1/0" if queue == "valkey" else "",
        ),
        knowledge=KnowledgeSettings(
            enabled=knowledge,
            vector_backend=vectors,
            sparse="off",
            embedding_model="text-embedding-3-small",
            embedding_api_key="k",
            embedding_base_url="http://127.0.0.1:1",
        ),
    )


def _engine(settings: Settings) -> Any:
    """The engine the process would hand over; it opens nothing until first use."""
    if settings.storage.backend != "postgres":
        return None
    return db_module.build_engine(_URL)


def _composer_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == _COMPOSER and record.levelno >= logging.WARNING
    ]


def test_the_in_memory_index_behind_valkey_workers_refuses_to_start() -> None:
    settings = _settings(storage="postgres", queue="valkey")

    with pytest.raises(RuntimeError) as refused:
        build_knowledge_context(settings, engine=_engine(settings))

    message = str(refused.value)
    assert f"{_REFUSAL} with INQTRIX_QUEUE_BACKEND=valkey" in message
    assert "documents that the API process never sees" in message
    assert _REMEDY in message


@pytest.mark.parametrize("storage", ["memory", "postgres"])
def test_the_in_memory_index_across_replicas_refuses_to_start(storage: str) -> None:
    settings = _settings(storage=storage, replicas=2)

    with pytest.raises(RuntimeError) as refused:
        build_knowledge_context(settings, engine=_engine(settings))

    message = str(refused.value)
    assert f"{_REFUSAL} across INQTRIX_REPLICA_COUNT=2 replicas" in message
    assert "finds only the documents it indexed itself" in message
    assert _REMEDY in message


def test_the_in_memory_index_beside_postgres_warns_once_at_startup(caplog) -> None:
    settings = _settings(storage="postgres")

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        context = build_knowledge_context(settings, engine=_engine(settings))

    assert context is not None
    assert isinstance(context.store, PostgresKnowledgeStore)
    (warning,) = _composer_warnings(caplog)
    assert warning.levelno == logging.WARNING
    message = warning.getMessage()
    assert message.startswith(_VOLATILE)
    assert "The index is volatile and has no size limit" in message
    assert "it is empty after every restart" in message
    assert "until its collection is rebuilt" in message
    assert "Use it for trials only" in message
    assert "set INQTRIX_VECTOR_BACKEND=qdrant" in message


def test_one_process_without_postgres_is_a_quiet_trial(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        context = build_knowledge_context(_settings())

    assert context is not None
    assert isinstance(context.store, MemoryKnowledgeStore)
    assert _composer_warnings(caplog) == []


def test_qdrant_behind_workers_and_replicas_is_neither_refused_nor_warned_about(
    monkeypatch, caplog
) -> None:
    built: list[dict[str, Any]] = []

    class _QdrantIndexStandIn:
        """Takes the Qdrant index's place: no client, no connection."""

        supports_hybrid = False

        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

    monkeypatch.setattr(
        "inqtrix.knowledge.stores.qdrant_store.QdrantVectorIndex", _QdrantIndexStandIn
    )
    settings = _settings(storage="postgres", queue="valkey", replicas=3, vectors="qdrant")

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        context = build_knowledge_context(settings, engine=_engine(settings))

    assert context is not None
    assert len(built) == 1
    assert _composer_warnings(caplog) == []


def test_a_disabled_knowledge_engine_leaves_workers_and_replicas_alone(caplog) -> None:
    """The worker stack without knowledge keeps the vector backend at its default."""
    settings = _settings(storage="postgres", queue="valkey", replicas=3, knowledge=False)

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        context = build_knowledge_context(settings, engine=_engine(settings))

    assert context is None
    assert _composer_warnings(caplog) == []
