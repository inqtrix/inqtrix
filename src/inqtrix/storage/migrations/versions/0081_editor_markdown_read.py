"""Re-stamp collaboration documents after the Markdown read rule changed.

Revision ID: 0081_editor_markdown_read
Revises: 0080_assistant_edit_comment_kind

``parseEditorMarkdown`` no longer applies the LaTeX import rule. That rule
belongs on FOREIGN Markdown; applied to the editor's own serialisation it
destroyed content — an escaped ``\\[Marke\\]`` became a formula block and
tore the paragraph apart.

The behaviour token ``EDITOR_SCHEMA_BEHAVIOR_INPUTS.markdownProjection``
moves with it, because BOTH sides call the function: the sidecar on
activation and on publication, the browser in the suggestion path. A skew
between them would build two different documents from the same Markdown,
and an unchanged fingerprint would let that skew pass unseen — which is the
one thing the fingerprint exists to prevent.

Moving the token changes the schema fingerprint, and every stored document
carries the old one. Without this migration each of them would be refused
on load with ``invalid_schema``. Re-stamping is correct rather than
generous: the changed rule governs the Markdown -> Y.Doc CONVERSION, which
runs once at activation. A stored document is a binary CRDT structure and
is never rebuilt from Markdown, so nothing about it is invalidated.

Shape follows ``0052_editor_review_semantics``, which re-stamped the same
two columns the last time the fingerprint moved.
"""

from __future__ import annotations

from alembic import op

revision = "0081_editor_markdown_read"
down_revision = "0080_assistant_edit_comment_kind"
branch_labels = None
depends_on = None

_OLD_SCHEMA_HASH = (
    "f1d070c4d259bcf01ca58116aecd6547275a75c176a96ef8e33f7f9123f03084"
)
_NEW_SCHEMA_HASH = (
    "eaf0b14b6d9809e82ea21de3d07c01b7d065ef4f3b9bc682ee8f0a1549000faf"
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
