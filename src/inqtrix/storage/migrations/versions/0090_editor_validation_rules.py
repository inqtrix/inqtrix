"""Re-stamp collaboration documents after the editor's validation rules changed.

Revision ID: 0090_editor_validation_rules
Revises: 0089_prepared_parser_revision

Three rules that decide which collaborative documents and updates are valid
became stricter, and each is part of the schema fingerprint:

- Text alignment accepts only the offered alignments (left, center, right,
  justify, or none), for every writer. The value is rendered into a style
  attribute, so any other value would inject style text into every reader's
  view.
- The canonical Yjs comparison reads attribute names from the attribute map
  itself, so an attribute named ``__proto__`` cannot travel hidden in a
  document.
- The suggest-mode transform refuses changes the collaboration service would
  refuse (a new block that takes existing content along, a change to another
  person's suggestion, a suggestion removed instead of withdrawn) with a
  reason code.

Every stored document carries the previous fingerprint. Without this
migration each of them would be refused on load with ``invalid_schema``.
Re-stamping is correct rather than generous: the editor's own commands and
its HTML reading only ever produce an offered alignment, and it never writes
such an attribute name, so no document written through the editor changes
meaning. A stored document that holds anything else can only come from a
modified client; the stricter validation refuses it on load, which is the
intended fail-closed outcome.

Shape follows ``0081_editor_markdown_read``, which re-stamped the same two
columns the last time the fingerprint moved.
"""

from __future__ import annotations

from alembic import op

revision = "0090_editor_validation_rules"
down_revision = "0089_prepared_parser_revision"
branch_labels = None
depends_on = None

_OLD_SCHEMA_HASH = (
    "eaf0b14b6d9809e82ea21de3d07c01b7d065ef4f3b9bc682ee8f0a1549000faf"
)
_NEW_SCHEMA_HASH = (
    "fbefc3116855e8ae8ba8f24375d5e72a1b189bd532254ae115af12f378d84845"
)


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE editor_documents
        SET collaboration_schema_hash = '{_NEW_SCHEMA_HASH}'
        WHERE content_mode = 'collaboration'
          AND collaboration_schema_hash = '{_OLD_SCHEMA_HASH}'
        """
    )
    op.execute(
        f"""
        UPDATE editor_collaboration_snapshots
        SET schema_hash = '{_NEW_SCHEMA_HASH}'
        WHERE schema_hash = '{_OLD_SCHEMA_HASH}'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE editor_documents
        SET collaboration_schema_hash = '{_OLD_SCHEMA_HASH}'
        WHERE content_mode = 'collaboration'
          AND collaboration_schema_hash = '{_NEW_SCHEMA_HASH}'
        """
    )
    op.execute(
        f"""
        UPDATE editor_collaboration_snapshots
        SET schema_hash = '{_OLD_SCHEMA_HASH}'
        WHERE schema_hash = '{_NEW_SCHEMA_HASH}'
        """
    )
