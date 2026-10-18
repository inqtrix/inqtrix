"""Gate and URL-translation behaviour of the workspace-agent checkpointer.

The gate decides Agent-Desk visibility (features.workspace_agent) from
settings and imports alone; the URL translation feeds the psycopg pool
that opens lazily on the first agent run. Both had zero test coverage
while being the two ways a deployment silently loses the Agent Desk or
crashes its first run.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.checkpointing import (
    CheckpointerHandle,
    _psycopg_conninfo,
    build_checkpointer_handle,
)
from inqtrix.settings import (
    AgentPlatformSettings,
    AuthSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


def _memory_settings(**agent_kwargs) -> Settings:
    return Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(backend="memory", database_url=""),
        auth=AuthSettings(mode="none"),
        agent_platform=AgentPlatformSettings(**agent_kwargs),
    )


def test_psycopg_conninfo_strips_asyncpg_only_query_params() -> None:
    # The bundled pgbouncer URL appends prepared_statement_cache_size=0
    # (asyncpg's transaction-pooling mitigation). libpq rejects unknown
    # URI parameters, so surviving the scheme swap would fail the FIRST
    # agent run at pool open — while the Agent Desk stays visible.
    url = (
        "postgresql+asyncpg://inqtrix:pw@stack-pgbouncer:6432/inqtrix"
        "?prepared_statement_cache_size=0"
    )
    assert _psycopg_conninfo(url) == (
        "postgresql://inqtrix:pw@stack-pgbouncer:6432/inqtrix"
    )
    plain = "postgresql+asyncpg://u:p@db:5432/inqtrix"
    assert _psycopg_conninfo(plain) == "postgresql://u:p@db:5432/inqtrix"


def test_psycopg_conninfo_keeps_libpq_parameters() -> None:
    # Only the asyncpg-only parameters go; libpq parameters such as
    # sslmode must pass through for managed/TLS-requiring databases.
    url = (
        "postgresql+asyncpg://u:p@db:5432/inqtrix"
        "?sslmode=require&prepared_statement_cache_size=0"
        "&statement_cache_size=0"
    )
    assert _psycopg_conninfo(url) == (
        "postgresql://u:p@db:5432/inqtrix?sslmode=require"
    )


def test_gate_names_the_missing_durable_backend(caplog) -> None:
    # The treacherous branch: agent enabled, but no postgres backend and
    # no volatile opt-in. Returning None SILENTLY would make the Agent
    # Desk vanish with no log line to grep for.
    with caplog.at_level("WARNING", logger="inqtrix"):
        handle = build_checkpointer_handle(
            _memory_settings(enabled=True, allow_volatile=False)
        )
    assert handle is None
    assert any(
        "features.workspace_agent remains false" in record.message
        and "durable checkpointer" in record.message
        for record in caplog.records
    )


def test_gate_logs_the_explicit_opt_out(caplog) -> None:
    # An operator's INQTRIX_AGENT_ENABLED=false is intended, so it logs
    # at INFO — but it must still be findable during a "why is the Agent
    # Desk missing" hunt.
    with caplog.at_level("INFO", logger="inqtrix"):
        handle = build_checkpointer_handle(_memory_settings(enabled=False))
    assert handle is None
    assert any(
        "INQTRIX_AGENT_ENABLED=false" in record.message
        for record in caplog.records
    )


def test_volatile_escape_yields_a_non_durable_handle() -> None:
    # The documented escape: no postgres, but explicit volatile opt-in
    # registers the agent with a non-durable checkpointer.
    handle = build_checkpointer_handle(
        _memory_settings(enabled=True, allow_volatile=True)
    )
    assert handle is not None
    assert handle.durable is False


def test_strict_thread_delete_verifies_the_checkpoint_is_absent() -> None:
    class Saver:
        def __init__(self) -> None:
            self.remaining = object()

        def delete_thread(self, _thread_id: str) -> None:
            return None

        def get_tuple(self, _config):
            return self.remaining

    handle = CheckpointerHandle(database_url=None)
    saver = Saver()
    handle._saver = saver
    with pytest.raises(RuntimeError, match="checkpoint lineage remains"):
        handle.delete_thread_strict("run_1")

    saver.remaining = None
    handle.delete_thread_strict("run_1")


def test_pool_size_flows_from_settings_to_the_handle() -> None:
    """One source, three displays: the pool ceiling must come from
    INQTRIX_AGENT_CHECKPOINTER_POOL_SIZE, not from a class literal."""
    settings = Settings(
        server=ServerSettings(public_base_url=""),
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://u:p@db:5432/inqtrix",
        ),
        auth=AuthSettings(mode="none"),
        agent_platform=AgentPlatformSettings(checkpointer_pool_size=9),
    )
    handle = build_checkpointer_handle(settings)
    assert handle is not None
    assert handle.max_connections == 9
    handle.close()


def test_pool_size_rejects_a_nonpositive_value() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        AgentPlatformSettings(checkpointer_pool_size=0)


def test_volatile_handle_declares_zero_server_connections() -> None:
    """An InMemorySaver opens no server connections; the attribute must
    say so instead of inheriting the durable default."""
    handle = build_checkpointer_handle(
        _memory_settings(enabled=True, allow_volatile=True)
    )
    assert handle is not None
    assert handle.durable is False
    assert handle.max_connections == 0
