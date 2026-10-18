"""Alembic environment for the inqtrix platform schema (async).

Online migrations accept only a live connection injected by the managed
``inqtrix-migrate`` runner. That boundary keeps privilege preflight, advisory
and table locks, temporary owner maintenance, Alembic, and postconditions in
one PostgreSQL transaction.

Offline SQL rendering is deliberately unsupported. Several revisions perform
data-dependent validation and reconciliation that cannot be represented as a
truthful connection-free SQL artifact.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy.engine import Connection

from inqtrix.storage.content_orm import content_metadata
from inqtrix.storage.runs_orm import runs_metadata
from inqtrix.storage.auth_orm import auth_metadata
from inqtrix.storage.identity_orm import identity_metadata
from inqtrix.storage.indexing_orm import indexing_metadata
from inqtrix.storage.knowledge_orm import knowledge_metadata
from inqtrix.storage.chat_orm import chat_metadata
from inqtrix.storage.editor_orm import editor_metadata
from inqtrix.storage.asset_records_orm import asset_metadata
from inqtrix.storage.vector_index_orm import vector_index_metadata
from inqtrix.storage.account_orm import account_metadata
from inqtrix.storage.agent_memory_orm import agent_memory_metadata
from inqtrix.storage.editor_collaboration_orm import editor_collaboration_metadata
from inqtrix.storage.source_lifecycle_orm import source_lifecycle_metadata

config = context.config

target_metadata = [
    identity_metadata,
    content_metadata,
    runs_metadata,
    auth_metadata,
    knowledge_metadata,
    indexing_metadata,
    chat_metadata,
    editor_metadata,
    asset_metadata,
    vector_index_metadata,
    account_metadata,
    agent_memory_metadata,
    editor_collaboration_metadata,
    source_lifecycle_metadata,
]


def run_migrations_offline() -> None:
    """Reject incomplete connection-free migration rendering."""
    raise RuntimeError(
        "Alembic offline SQL is unsupported: Inqtrix migrations include "
        "data-dependent validation and reconciliation. Run inqtrix-migrate "
        "against the target database so locks, data changes, and "
        "postconditions share one transaction."
    )


def do_run_migrations(connection: Connection) -> None:
    """Run every requested revision inside the caller's transaction."""
    version_table_schema = config.attributes.get("version_table_schema")
    version_options = (
        {"version_table_schema": version_table_schema}
        if isinstance(version_table_schema, str) and version_table_schema
        else {}
    )
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        transactional_ddl=True,
        transaction_per_migration=False,
        on_version_apply=config.attributes.get("on_version_apply"),
        **version_options,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations only through the managed connection-injection path."""
    connection = config.attributes.get("connection", None)
    if connection is not None:
        do_run_migrations(connection)
        return
    raise RuntimeError(
        "Online migrations must run through inqtrix-migrate so role, RLS, "
        "locking, and postcondition checks share the Alembic transaction."
    )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
