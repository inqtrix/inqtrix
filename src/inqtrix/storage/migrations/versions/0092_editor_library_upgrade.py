"""Re-stamp collaboration documents after the editor libraries moved.

Revision ID: 0092_editor_library_upgrade
Revises: 0091_rejected_by_edit

The schema fingerprint names the exact versions of the editor libraries, so
moving Tiptap from 3.27.1 to 3.31.3 and its Yjs binding (``@tiptap/y-tiptap``)
from 3.0.6 to 3.0.9 changes it. The new Tiptap also has one more HTML parse
rule for table cells and table header cells (an empty cell read from HTML
gets the cell's default content), and the fingerprint counts parse rules.
With the same move the Markdown reader got one rule of its own: the new
serializer writes a line break inside a table cell as ``<br>``, and the
reader takes exactly that tag inside a table cell as the hard break it
stands for, also where no DOM exists; the fingerprint's Markdown behavior
token names the rule.

Every stored document carries the previous fingerprint. Without this
migration each of them would be refused on load with ``invalid_schema``.
Re-stamping is correct rather than generous: the nodes, marks and attributes
of the schema are unchanged, the extra parse rule only applies when HTML is
read (pasting), and the binding writes the same element, text and attribute
structure into Yjs. What changed in the binding is how the local caret is
restored after a remote change, how remote carets are drawn, how reordered
blocks are written (the blocks that did not move keep their Yjs items), and
which local changes the undo history captures; none of it changes what a
stored document means or how the service validates it. The new Tiptap also
reads and writes some Markdown differently (for example, a line break inside
a table cell is written as ``<br>``); that applies to Markdown read or written
from now on, not to a stored Yjs document.

Shape follows ``0090_editor_validation_rules``, which re-stamped the same
two columns the last time the fingerprint moved.
"""

from __future__ import annotations

from alembic import op

revision = "0092_editor_library_upgrade"
down_revision = "0091_rejected_by_edit"
branch_labels = None
depends_on = None

_OLD_SCHEMA_HASH = (
    "fbefc3116855e8ae8ba8f24375d5e72a1b189bd532254ae115af12f378d84845"
)
_NEW_SCHEMA_HASH = (
    "d50b3d9356b6d40e0e3ed76e052892912850844f692dec63cdd83fbb1de31c9e"
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
