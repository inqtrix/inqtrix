"""Migration 0098 moves owned rows without a project namespace into their owner's.

A signed-in script that wrote without the namespace header left its rows with
``workspace_id`` NULL, where the UI never lists them. The revision moves them
once, keeps rows that belong together in one namespace, and leaves what it
must not touch.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import pytest
import pytest_asyncio
from alembic import command
from sqlalchemy import text

from inqtrix.storage.db import build_engine
from inqtrix.storage.migrate import build_alembic_config

URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.postgres

PREVIOUS = "0097_scim_users"
HEAD = "0098_project_namespace_backfill"
MIGRATION_LOGGER = f"inqtrix.storage.migrations.versions.{HEAD}"
ISSUER = "https://idp.example.invalid"
NAMESPACE = re.compile(r"ws_[0-9a-f]{32}")


@pytest_asyncio.fixture()
async def isolated_schema():
    engine = build_engine(URL, null_pool=True)
    schema = f"inqtrix_ns_{uuid.uuid4().hex}"
    async with engine.begin() as connection:
        privileged = (await connection.execute(text(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ))).scalar_one()
        assert privileged, "Migration fixture needs a superuser or BYPASSRLS connection"
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        yield engine, schema
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


async def migrate(fixture, revision, *, downgrade=False):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))

        def apply(sync):
            config = build_alembic_config(URL)
            config.attributes["connection"] = sync
            config.attributes["version_table_schema"] = schema
            (command.downgrade if downgrade else command.upgrade)(config, revision)

        await connection.run_sync(apply)


async def execute(fixture, statement, parameters=None):
    engine, schema = fixture
    async with engine.begin() as connection:
        await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        result = await connection.execute(text(statement), parameters or {})
        return result.all() if result.returns_rows else []


async def add_user(fixture, namespace=None):
    user_id = uuid.uuid4()
    await execute(
        fixture,
        "INSERT INTO users (id, tenant_id, issuer, subject, email, default_workspace_id) "
        "VALUES (:id, 'default', :issuer, :subject, :email, :namespace)",
        {
            "id": user_id,
            "issuer": ISSUER,
            "subject": str(user_id),
            "email": f"{user_id}@example.invalid",
            "namespace": namespace,
        },
    )
    return user_id


async def namespace_of(fixture, user_id):
    rows = await execute(
        fixture, "SELECT default_workspace_id FROM users WHERE id = :id", {"id": user_id}
    )
    return rows[0][0]


async def workspace(fixture, table, key, value):
    rows = await execute(
        fixture, f"SELECT workspace_id FROM {table} WHERE {key} = :value", {"value": value}
    )
    return rows[0][0]


async def seed_owned_rows(fixture, owner, suffix):
    """One row without a namespace in each table the UI lists by namespace."""
    params = {"owner": owner}
    for statement in (
        f"INSERT INTO editor_folders (id, created_by_user_id, title, created_at, updated_at) "
        f"VALUES ('folder-{suffix}', :owner, 'Folder', 1, 1)",
        f"INSERT INTO editor_documents (id, created_by_user_id, folder_id, created_at, updated_at) "
        f"VALUES ('doc-{suffix}', :owner, 'folder-{suffix}', 1, 1)",
        f"INSERT INTO chat_thread_groups (id, created_by_user_id, title, created_at, updated_at) "
        f"VALUES ('chat-group-{suffix}', :owner, 'Group', 1, 1)",
        f"INSERT INTO chat_threads (id, created_by_user_id, group_id, created_at, updated_at) "
        f"VALUES ('thread-{suffix}', :owner, 'chat-group-{suffix}', 1, 1)",
        f"INSERT INTO agent_session_groups (id, created_by_user_id, created_at, updated_at) "
        f"VALUES ('agent-group-{suffix}', :owner, 1, 1)",
        f"INSERT INTO agent_sessions (id, created_by_user_id, group_id, created_at, updated_at) "
        f"VALUES ('agent-session-{suffix}', :owner, 'agent-group-{suffix}', 1, 1)",
        f"INSERT INTO knowledge_session_groups (id, created_by_user_id, created_at, updated_at) "
        f"VALUES ('knowledge-group-{suffix}', :owner, 1, 1)",
        f"INSERT INTO knowledge_sessions (id, created_by_user_id, group_id, created_at, updated_at) "
        f"VALUES ('knowledge-session-{suffix}', :owner, 'knowledge-group-{suffix}', 1, 1)",
        f"INSERT INTO vector_index_records (id, created_by_user_id, model, created_at, updated_at) "
        f"VALUES ('index-{suffix}', :owner, 'model', 1, 1)",
        f"INSERT INTO runs (run_id, created_by_user_id, question, created_at) "
        f"VALUES ('run-{suffix}', :owner, 'question', 1)",
        f"INSERT INTO indexing_jobs (job_id, created_by_user_id, collection_id, generation_id, "
        f"created_at) VALUES ('job-{suffix}', :owner, 'collection-{suffix}', 'generation-{suffix}', 1)",
        f"INSERT INTO files (id, owner_user_id, file_name, content_type, size_bytes, sha256, "
        f"object_key, created_at) VALUES ('file-{suffix}', :owner, 'a.txt', 'text/plain', 1, "
        f"'00', 'key-{suffix}', 1)",
    ):
        await execute(fixture, statement, params)


OWNED_ROWS = (
    ("editor_folders", "id", "folder-{}"),
    ("editor_documents", "id", "doc-{}"),
    ("chat_thread_groups", "id", "chat-group-{}"),
    ("chat_threads", "id", "thread-{}"),
    ("agent_session_groups", "id", "agent-group-{}"),
    ("agent_sessions", "id", "agent-session-{}"),
    ("knowledge_session_groups", "id", "knowledge-group-{}"),
    ("knowledge_sessions", "id", "knowledge-session-{}"),
    ("vector_index_records", "id", "index-{}"),
    ("runs", "run_id", "run-{}"),
    ("indexing_jobs", "job_id", "job-{}"),
    ("files", "id", "file-{}"),
)


async def add_section(fixture, section_id, owner, role, namespace=None):
    await execute(
        fixture,
        "INSERT INTO asset_sections (id, created_by_user_id, workspace_id, kind, title, "
        "semantic_role, created_at, updated_at) "
        "VALUES (:id, :owner, :namespace, 'custom', 'Section', :role, 1, 1)",
        {"id": section_id, "owner": owner, "namespace": namespace, "role": role},
    )


async def add_asset(fixture, asset_id, owner, section_id, group_id=None, namespace=None):
    await execute(
        fixture,
        "INSERT INTO asset_records (id, created_by_user_id, workspace_id, section_id, group_id, "
        "created_at, updated_at) VALUES (:id, :owner, :namespace, :section, :group_id, 1, 1)",
        {
            "id": asset_id,
            "owner": owner,
            "namespace": namespace,
            "section": section_id,
            "group_id": group_id,
        },
    )


@pytest.mark.asyncio
async def test_owned_rows_move_into_the_namespace_of_their_owner(isolated_schema):
    # The script's rows land where the UI of their owner lists them; an owner
    # without a namespace gets one of the browser's form, which a later
    # session start returns. Rows without an owner and the usage ledger stay.
    await migrate(isolated_schema, PREVIOUS)
    newcomer = await add_user(isolated_schema)
    regular = await add_user(isolated_schema, "ws_regular0000000000000000000000001")
    bystander = await add_user(isolated_schema)
    await seed_owned_rows(isolated_schema, newcomer, "n")
    await seed_owned_rows(isolated_schema, regular, "r")
    await execute(
        isolated_schema,
        "INSERT INTO editor_documents (id, created_at, updated_at) VALUES ('doc-ownerless', 1, 1)",
    )
    await execute(
        isolated_schema,
        "INSERT INTO llm_usage (tenant_id, user_id, feature, operation, model, input_tokens, "
        "output_tokens, request_count, duration_ms, outcome, created_at) "
        "VALUES ('default', :owner, 'chat', 'chat', 'model', 1, 1, 1, 1, 'success', 1)",
        {"owner": newcomer},
    )

    await migrate(isolated_schema, HEAD)

    claimed = await namespace_of(isolated_schema, newcomer)
    assert claimed is not None and NAMESPACE.fullmatch(claimed)
    assert await namespace_of(isolated_schema, regular) == "ws_regular0000000000000000000000001"
    assert await namespace_of(isolated_schema, bystander) is None
    for table, key, pattern in OWNED_ROWS:
        assert await workspace(isolated_schema, table, key, pattern.format("n")) == claimed, table
        assert (
            await workspace(isolated_schema, table, key, pattern.format("r"))
            == "ws_regular0000000000000000000000001"
        ), table
    assert await workspace(isolated_schema, "editor_documents", "id", "doc-ownerless") is None
    ledger = await execute(isolated_schema, "SELECT workspace_id FROM llm_usage")
    assert ledger == [(None,)]


@pytest.mark.asyncio
async def test_a_prepared_section_merges_into_its_twin_with_its_groups_and_assets(isolated_schema):
    # The script's default section and the UI's one of the same role become
    # one; the duplicate is gone and nothing in it is lost.
    await migrate(isolated_schema, PREVIOUS)
    owner = await add_user(isolated_schema, "ws_owner000000000000000000000000001")
    await add_section(isolated_schema, "twin", owner, "library", "ws_owner000000000000000000000000001")
    await add_section(isolated_schema, "legacy", owner, "library")
    await execute(
        isolated_schema,
        "INSERT INTO asset_groups (id, created_by_user_id, section_id, title, created_at, updated_at) "
        "VALUES ('group', :owner, 'legacy', 'Group', 1, 1)",
        {"owner": owner},
    )
    await add_asset(isolated_schema, "asset", owner, "legacy", "group")

    await migrate(isolated_schema, HEAD)

    sections = await execute(isolated_schema, "SELECT id FROM asset_sections ORDER BY id")
    assert sections == [("twin",)]
    for table in ("asset_groups", "asset_records"):
        rows = await execute(isolated_schema, f"SELECT section_id, workspace_id FROM {table}")
        assert rows == [("twin", "ws_owner000000000000000000000000001")], table


@pytest.mark.asyncio
async def test_a_twinned_section_a_running_deletion_reaches_stays_with_its_operation(
    isolated_schema, caplog
):
    # Merging would delete what the deletion works on: the section, its asset
    # and the operation keep the namespace they had.
    await migrate(isolated_schema, PREVIOUS)
    owner = await add_user(isolated_schema, "ws_owner000000000000000000000000002")
    await add_section(isolated_schema, "twin", owner, "temporary", "ws_owner000000000000000000000000002")
    await add_section(isolated_schema, "held", owner, "temporary")
    await add_asset(isolated_schema, "asset", owner, "held")
    await execute(
        isolated_schema,
        "INSERT INTO deletion_operations (operation_id, target_kind, target_id, status, "
        "created_by_user_id, created_at, updated_at) "
        "VALUES ('deletion', 'section', 'held', 'running', :owner, 1, 1)",
        {"owner": owner},
    )
    await execute(
        isolated_schema,
        "INSERT INTO deletion_operation_assets (operation_id, asset_id, created_by_user_id) "
        "VALUES ('deletion', 'asset', :owner)",
        {"owner": owner},
    )

    with caplog.at_level(logging.INFO, logger=MIGRATION_LOGGER):
        await migrate(isolated_schema, HEAD)

    assert await workspace(isolated_schema, "asset_sections", "id", "held") is None
    assert await workspace(isolated_schema, "asset_records", "id", "asset") is None
    assert await workspace(isolated_schema, "deletion_operations", "operation_id", "deletion") is None
    assert await workspace(isolated_schema, "deletion_operation_assets", "operation_id", "deletion") is None
    # What the revision leaves out is said, with the count.
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("Asset sections left without a project namespace" in line and line.endswith(": 1.") for line in warnings)
    assert any("Rows in deletion_operations left without" in line and line.endswith(": 1.") for line in warnings)


@pytest.mark.asyncio
async def test_a_running_operation_with_a_twin_in_the_namespace_stays(isolated_schema, caplog):
    # Two running uploads of one asset cannot share a namespace; the one
    # without a namespace stays, a finished one moves.
    await migrate(isolated_schema, PREVIOUS)
    owner = await add_user(isolated_schema, "ws_owner000000000000000000000000003")
    await add_section(isolated_schema, "section", owner, None, "ws_owner000000000000000000000000003")
    await add_asset(isolated_schema, "asset", owner, "section", namespace="ws_owner000000000000000000000000003")
    for operation_id, namespace, status in (
        ("running-legacy", None, "running"),
        ("running-twin", "ws_owner000000000000000000000000003", "running"),
        ("finished-legacy", None, "ready"),
    ):
        await execute(
            isolated_schema,
            "INSERT INTO upload_operations (operation_id, asset_id, file_id, file_manifest, binding, "
            "status, stage, created_by_user_id, workspace_id, created_at, updated_at) "
            "VALUES (:id, 'asset', 'file', '[]', '{}', :status, 'prepared', :owner, :namespace, 1, 1)",
            {"id": operation_id, "status": status, "owner": owner, "namespace": namespace},
        )

    with caplog.at_level(logging.INFO, logger=MIGRATION_LOGGER):
        await migrate(isolated_schema, HEAD)

    assert await workspace(isolated_schema, "upload_operations", "operation_id", "running-legacy") is None
    assert (
        await workspace(isolated_schema, "upload_operations", "operation_id", "finished-legacy")
        == "ws_owner000000000000000000000000003"
    )
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert any("Rows in upload_operations left without" in line and line.endswith(": 1.") for line in warnings)


@pytest.mark.asyncio
async def test_a_source_lifecycle_moves_with_its_key_unless_the_namespace_has_it(isolated_schema):
    await migrate(isolated_schema, PREVIOUS)
    owner = await add_user(isolated_schema, "ws_owner000000000000000000000000004")
    for source_id, key, namespace in (
        ("asset:moving", "", None),
        ("asset:both", "", None),
        ("asset:both", "ws_owner000000000000000000000000004", "ws_owner000000000000000000000000004"),
    ):
        await execute(
            isolated_schema,
            "INSERT INTO source_lifecycles (tenant_id, source_id, owner_key, workspace_key, "
            "owner_user_id, workspace_id, updated_at) "
            "VALUES ('default', :source, :owner_key, :key, :owner, :namespace, 1)",
            {
                "source": source_id,
                "owner_key": str(owner),
                "key": key,
                "owner": owner,
                "namespace": namespace,
            },
        )

    await migrate(isolated_schema, HEAD)

    rows = await execute(
        isolated_schema,
        "SELECT source_id, workspace_key, workspace_id FROM source_lifecycles "
        "ORDER BY source_id, workspace_key",
    )
    assert rows == [
        ("asset:both", "", None),
        (
            "asset:both",
            "ws_owner000000000000000000000000004",
            "ws_owner000000000000000000000000004",
        ),
        (
            "asset:moving",
            "ws_owner000000000000000000000000004",
            "ws_owner000000000000000000000000004",
        ),
    ]


@pytest.mark.asyncio
async def test_a_knowledge_document_takes_the_namespace_of_its_asset(isolated_schema):
    # A document indexed from an asset follows the asset; a source managed
    # through the API stays bound to its collection without a namespace.
    await migrate(isolated_schema, PREVIOUS)
    owner = await add_user(isolated_schema)
    await add_section(isolated_schema, "section", owner, None)
    await add_asset(isolated_schema, "asset", owner, "section")
    await execute(
        isolated_schema,
        "INSERT INTO knowledge_collections (id, name, embedding_model, embedding_dim, created_at) "
        "VALUES ('collection', 'Collection', 'model', 3, 1)",
    )
    for document_id, source_id in (("from-asset", "asset:asset"), ("from-api", "api:document")):
        await execute(
            isolated_schema,
            "INSERT INTO knowledge_documents (id, collection_id, title, created_at, source_id, "
            "source_owner_user_id, source_scope_bound) "
            "VALUES (:id, 'collection', 'Document', 1, :source, :owner, true)",
            {"id": document_id, "source": source_id, "owner": owner},
        )

    await migrate(isolated_schema, HEAD)

    claimed = await namespace_of(isolated_schema, owner)
    rows = await execute(
        isolated_schema,
        "SELECT id, source_workspace_id FROM knowledge_documents ORDER BY id",
    )
    assert rows == [("from-api", None), ("from-asset", claimed)]


@pytest.mark.asyncio
async def test_without_rows_to_move_the_revision_changes_nothing_and_the_downgrade_keeps_it(
    isolated_schema,
):
    await migrate(isolated_schema, PREVIOUS)
    user = await add_user(isolated_schema)

    await migrate(isolated_schema, HEAD)
    assert await namespace_of(isolated_schema, user) is None

    await migrate(isolated_schema, PREVIOUS, downgrade=True)
    assert await namespace_of(isolated_schema, user) is None
