"""Move owned rows without a project namespace into their owner's namespace.

Revision ID: 0098_project_namespace_backfill
Revises: 0097_scim_users

Before this revision, a signed-in client that wrote without the
``X-Inqtrix-Workspace-Id`` header (a script with a personal access token)
stored its rows with ``workspace_id`` NULL. The UI lists with the person's
canonical project namespace (``users.default_workspace_id``) and strict
equality, so such rows never appeared there, and rows that belong together
(a session and its runs, a section and its assets) can now end up in
different namespaces once new writes take the canonical one. This revision
moves every owned row with a NULL namespace into its owner's namespace,
once:

* An owner without a namespace gets one of the form the browser creates
  (``ws_`` and 32 hex digits); a later session start returns it.
* A prepared asset section (``temporary``, ``library``,
  ``project_sources``) without a namespace that already has a twin of the
  same role in the owner's namespace is merged into the twin: its groups and
  assets move there and the empty duplicate is deleted. Such a section that a
  queued or running deletion reaches (the section, one of its groups or one
  of its assets) is left as it is, together with its groups, its assets and
  the operations on them, so the deletion finishes what was asked.
* A queued or running upload or deletion operation is moved only when no
  operation of the owner's namespace holds the same target; the others stay
  and are reported.
* A source lifecycle moves together with its key (``workspace_key`` is the
  namespace or ``''`` and part of the primary key), unless a lifecycle of the
  same source and owner already exists in the owner's namespace.
* A knowledge document backed by an asset takes the asset's new namespace as
  its source namespace, as revision 0069 derived it.

Rows without an owner (modes ``apikey`` and ``none``), the usage ledger
(``llm_usage``, a historical record), API-managed knowledge sources (bound to
their collection on purpose) and the membership workspaces (UUID columns)
stay as they are. Each step logs how many rows it moved; whatever it leaves
out logs a WARNING with the count. Without such rows the revision changes
nothing.

The downgrade changes nothing: which rows had no namespace before is not
recorded, so the move cannot be undone. Back up the database before the
upgrade.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0098_project_namespace_backfill"
down_revision = "0097_scim_users"
branch_labels = None
depends_on = None

# Alembic loads this file under another name; the import path keeps the
# records under ``inqtrix``, where the log filter shows them.
log = logging.getLogger("inqtrix.storage.migrations.versions.0098_project_namespace_backfill")

# Owned tables whose rows the UI lists by project namespace, with the column
# that names the owner. Order matters only for the knowledge documents below,
# which read the moved assets and source lifecycles.
_OWNED_TABLES: tuple[tuple[str, str], ...] = (
    ("editor_folders", "created_by_user_id"),
    ("editor_documents", "created_by_user_id"),
    ("chat_thread_groups", "created_by_user_id"),
    ("chat_threads", "created_by_user_id"),
    ("agent_session_groups", "created_by_user_id"),
    ("agent_sessions", "created_by_user_id"),
    ("knowledge_session_groups", "created_by_user_id"),
    ("knowledge_sessions", "created_by_user_id"),
    ("asset_sections", "created_by_user_id"),
    ("asset_groups", "created_by_user_id"),
    ("asset_records", "created_by_user_id"),
    ("files", "owner_user_id"),
    ("vector_index_records", "created_by_user_id"),
    ("runs", "created_by_user_id"),
    ("indexing_jobs", "created_by_user_id"),
)

_PREPARED_ROLES = "('temporary', 'library', 'project_sources')"
_ACTIVE_DELETION = "('queued', 'running')"
_ACTIVE_UPLOAD = "('running', 'queued', 'awaiting_bytes', 'upload_failed')"

_OWNERS_SQL = " UNION ".join(
    f"SELECT {owner} AS owner_id FROM {table} WHERE workspace_id IS NULL AND {owner} IS NOT NULL"
    for table, owner in (
        *_OWNED_TABLES,
        ("source_lifecycles", "owner_user_id"),
        ("upload_operations", "created_by_user_id"),
        ("deletion_operations", "created_by_user_id"),
    )
)

# Prepared sections without a namespace that have a twin in the owner's
# namespace and that a running deletion reaches: merging would delete what the
# deletion works on, so they stay as they are, together with their groups,
# assets and the operations on them.
_HELD_SECTIONS_SQL = f"""
CREATE TEMPORARY TABLE r0098_held_sections ON COMMIT DROP AS
SELECT section.id
FROM asset_sections AS section
JOIN users AS account ON account.id = section.created_by_user_id
WHERE section.workspace_id IS NULL
  AND section.semantic_role IN {_PREPARED_ROLES}
  AND EXISTS (
      SELECT 1 FROM asset_sections AS twin
      WHERE twin.tenant_id = section.tenant_id
        AND twin.created_by_user_id = section.created_by_user_id
        AND twin.semantic_role = section.semantic_role
        AND twin.workspace_id = account.default_workspace_id
  )
  AND EXISTS (
      SELECT 1 FROM deletion_operations AS operation
      WHERE operation.tenant_id = section.tenant_id
        AND operation.status IN {_ACTIVE_DELETION}
        AND (
            (operation.target_kind = 'section' AND operation.target_id = section.id)
            OR (operation.target_kind = 'group' AND operation.target_id IN (
                SELECT asset_group.id FROM asset_groups AS asset_group
                WHERE asset_group.section_id = section.id))
            OR (operation.target_kind = 'asset' AND operation.target_id IN (
                SELECT asset.id FROM asset_records AS asset
                WHERE asset.section_id = section.id))
        )
  )
"""

_MERGE_PAIRS_SQL = f"""
CREATE TEMPORARY TABLE r0098_merge_pairs ON COMMIT DROP AS
SELECT legacy.id AS legacy_id, twin.id AS twin_id
FROM asset_sections AS legacy
JOIN users AS account ON account.id = legacy.created_by_user_id
JOIN asset_sections AS twin
  ON twin.tenant_id = legacy.tenant_id
 AND twin.created_by_user_id = legacy.created_by_user_id
 AND twin.semantic_role = legacy.semantic_role
 AND twin.workspace_id = account.default_workspace_id
WHERE legacy.workspace_id IS NULL
  AND legacy.semantic_role IN {_PREPARED_ROLES}
  AND legacy.id NOT IN (SELECT id FROM r0098_held_sections)
"""

# The asset behind a knowledge document, as revision 0069 resolves it.
_DOCUMENT_ASSET_ID = """
COALESCE(
    CASE
        WHEN document.source_id LIKE 'asset:%'
            THEN NULLIF(substr(BTRIM(document.source_id), 7), '')
        ELSE NULL
    END,
    CASE
        WHEN BTRIM(document.metadata->>'fileId') LIKE 'asset:%'
            THEN NULLIF(substr(BTRIM(document.metadata->>'fileId'), 7), '')
        ELSE NULLIF(BTRIM(document.metadata->>'fileId'), '')
    END
)
"""


def _count(bind: sa.engine.Connection, statement: str) -> int:
    return int(bind.execute(sa.text(statement)).rowcount or 0)


def upgrade() -> None:
    bind = op.get_bind()

    claimed = _count(
        bind,
        f"""
        UPDATE users
        SET default_workspace_id = 'ws_' || replace(gen_random_uuid()::text, '-', '')
        WHERE default_workspace_id IS NULL
          AND id IN ({_OWNERS_SQL})
        """,
    )
    log.info("Project namespaces assigned to owners without one: %d.", claimed)

    bind.execute(sa.text(_HELD_SECTIONS_SQL))
    held = int(bind.execute(sa.text("SELECT count(*) FROM r0098_held_sections")).scalar_one())
    if held:
        log.warning(
            "Asset sections left without a project namespace because a running deletion "
            "reaches them while a twin of the same role exists: %d.",
            held,
        )

    bind.execute(sa.text(_MERGE_PAIRS_SQL))
    for table in ("asset_groups", "asset_records"):
        bind.execute(
            sa.text(
                f"""
                UPDATE {table}
                SET section_id = pair.twin_id
                FROM r0098_merge_pairs AS pair
                WHERE {table}.section_id = pair.legacy_id
                """
            )
        )
    merged = _count(
        bind,
        "DELETE FROM asset_sections WHERE id IN (SELECT legacy_id FROM r0098_merge_pairs)",
    )
    log.info("Prepared asset sections merged into their twin: %d.", merged)

    for table, owner in _OWNED_TABLES:
        held_filter = ""
        if table == "asset_sections":
            held_filter = "AND target_row.id NOT IN (SELECT id FROM r0098_held_sections)"
        elif table in ("asset_groups", "asset_records"):
            held_filter = "AND target_row.section_id NOT IN (SELECT id FROM r0098_held_sections)"
        moved = _count(
            bind,
            f"""
            UPDATE {table} AS target_row
            SET workspace_id = account.default_workspace_id
            FROM users AS account
            WHERE target_row.workspace_id IS NULL
              AND target_row.{owner} = account.id
              AND account.default_workspace_id IS NOT NULL
              {held_filter}
            """,
        )
        log.info("Rows moved into their owner's project namespace in %s: %d.", table, moved)

    held_assets = (
        "SELECT asset.id FROM asset_records AS asset "
        "WHERE asset.section_id IN (SELECT id FROM r0098_held_sections)"
    )
    held_groups = (
        "SELECT asset_group.id FROM asset_groups AS asset_group "
        "WHERE asset_group.section_id IN (SELECT id FROM r0098_held_sections)"
    )
    held_targets = {
        "upload_operations": f"AND target_row.asset_id NOT IN ({held_assets})",
        "deletion_operations": (
            "AND NOT ("
            "(target_row.target_kind = 'section' AND target_row.target_id IN (SELECT id FROM r0098_held_sections))"
            f" OR (target_row.target_kind = 'group' AND target_row.target_id IN ({held_groups}))"
            f" OR (target_row.target_kind = 'asset' AND target_row.target_id IN ({held_assets}))"
            ")"
        ),
    }
    for table, key, active in (
        ("upload_operations", "asset_id", _ACTIVE_UPLOAD),
        ("deletion_operations", "target_id", _ACTIVE_DELETION),
    ):
        kind_match = "AND twin.target_kind = target_row.target_kind" if table == "deletion_operations" else ""
        moved = _count(
            bind,
            f"""
            UPDATE {table} AS target_row
            SET workspace_id = account.default_workspace_id
            FROM users AS account
            WHERE target_row.workspace_id IS NULL
              AND target_row.created_by_user_id = account.id
              AND account.default_workspace_id IS NOT NULL
              {held_targets[table]}
              AND NOT (
                  target_row.status IN {active}
                  AND EXISTS (
                      SELECT 1 FROM {table} AS twin
                      WHERE twin.tenant_id = target_row.tenant_id
                        AND twin.{key} = target_row.{key}
                        {kind_match}
                        AND twin.created_by_user_id = target_row.created_by_user_id
                        AND twin.workspace_id = account.default_workspace_id
                        AND twin.status IN {active}
                  )
              )
            """,
        )
        log.info("Rows moved into their owner's project namespace in %s: %d.", table, moved)
        left = int(
            bind.execute(
                sa.text(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE workspace_id IS NULL AND created_by_user_id IS NOT NULL"
                )
            ).scalar_one()
        )
        if left:
            log.warning(
                "Rows in %s left without a project namespace because a running operation "
                "of the owner's namespace holds the same target, or because they work on a "
                "section kept above: %d.",
                table,
                left,
            )

    moved = _count(
        bind,
        """
        UPDATE source_lifecycles AS target_row
        SET workspace_id = account.default_workspace_id,
            workspace_key = account.default_workspace_id
        FROM users AS account
        WHERE target_row.workspace_id IS NULL
          AND target_row.workspace_key = ''
          AND target_row.owner_user_id = account.id
          AND account.default_workspace_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM source_lifecycles AS twin
              WHERE twin.tenant_id = target_row.tenant_id
                AND twin.source_id = target_row.source_id
                AND twin.owner_key = target_row.owner_key
                AND twin.workspace_key = account.default_workspace_id
          )
        """,
    )
    log.info("Rows moved into their owner's project namespace in source_lifecycles: %d.", moved)
    left = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM source_lifecycles "
                "WHERE workspace_id IS NULL AND owner_user_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if left:
        log.warning(
            "Source lifecycles left without a project namespace because the same source "
            "already has one in the owner's namespace: %d.",
            left,
        )

    moved = _count(
        bind,
        """
        UPDATE deletion_operation_assets AS item
        SET workspace_id = operation.workspace_id
        FROM deletion_operations AS operation
        WHERE item.operation_id = operation.operation_id
          AND item.workspace_id IS NULL
          AND operation.workspace_id IS NOT NULL
        """,
    )
    log.info("Rows moved into their owner's project namespace in deletion_operation_assets: %d.", moved)

    from_assets = _count(
        bind,
        f"""
        UPDATE knowledge_documents AS document
        SET source_workspace_id = asset.workspace_id
        FROM asset_records AS asset
        WHERE asset.tenant_id = document.tenant_id
          AND asset.id = {_DOCUMENT_ASSET_ID}
          AND document.source_workspace_id IS NULL
          AND document.source_owner_user_id = asset.created_by_user_id
          AND asset.workspace_id IS NOT NULL
        """,
    )
    from_lifecycles = _count(
        bind,
        """
        WITH unique_lifecycle AS (
            SELECT tenant_id, source_id,
                   min(owner_user_id::text)::uuid AS owner_user_id,
                   min(workspace_id) AS workspace_id
            FROM source_lifecycles
            WHERE source_id LIKE 'asset:%'
            GROUP BY tenant_id, source_id
            HAVING count(*) = 1
        )
        UPDATE knowledge_documents AS document
        SET source_workspace_id = lifecycle.workspace_id
        FROM unique_lifecycle AS lifecycle
        WHERE lifecycle.tenant_id = document.tenant_id
          AND lifecycle.source_id = document.source_id
          AND document.source_id LIKE 'asset:%'
          AND document.source_workspace_id IS NULL
          AND document.source_owner_user_id = lifecycle.owner_user_id
          AND lifecycle.workspace_id IS NOT NULL
        """,
    )
    log.info(
        "Knowledge documents that took the project namespace of their source: %d.",
        from_assets + from_lifecycles,
    )


def downgrade() -> None:
    """Nothing to undo: the rows that had no namespace are not recorded."""
