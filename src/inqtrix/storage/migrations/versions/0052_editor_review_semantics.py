"""Add bounded review summaries and migrate the compatible editor schema.

Revision ID: 0052_editor_review
Revises: 0051_editor_comments
"""

from alembic import op

revision = "0052_editor_review"
down_revision = "0051_editor_comments"
branch_labels = None
depends_on = None

_OLD_SCHEMA_HASH = (
    "9b3e99c08e622f1c99cd04df4efeb639c4ea6c9553b526c51b4854f48964b235"
)
_NEW_SCHEMA_HASH = (
    "f1d070c4d259bcf01ca58116aecd6547275a75c176a96ef8e33f7f9123f03084"
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        ADD COLUMN change_summary jsonb NOT NULL
            DEFAULT jsonb_build_object(
                'edits', jsonb_build_array(),
                'omitted_edit_count', 0
            )
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        ADD COLUMN decision_outcome text NULL
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        ADD CONSTRAINT ck_collaboration_updates_change_summary
        CHECK (jsonb_typeof(change_summary) = 'object')
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        ADD CONSTRAINT ck_collaboration_updates_decision_outcome
        CHECK (
            decision_outcome IS NULL
            OR decision_outcome IN ('accepted', 'rejected')
        )
        """
    )
    op.execute(
        f"""
        UPDATE editor_documents
        SET collaboration_schema_version = 2,
            collaboration_schema_hash = '{_NEW_SCHEMA_HASH}'
        WHERE content_mode = 'collaboration'
          AND collaboration_schema_version = 1
          AND collaboration_schema_hash = '{_OLD_SCHEMA_HASH}'
        """
    )
    op.execute(
        f"""
        UPDATE editor_collaboration_snapshots
        SET schema_version = 2,
            schema_hash = '{_NEW_SCHEMA_HASH}'
        WHERE schema_version = 1
          AND schema_hash = '{_OLD_SCHEMA_HASH}'
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE editor_collaboration_snapshots
        SET schema_version = 1,
            schema_hash = '{_OLD_SCHEMA_HASH}'
        WHERE schema_version = 2
          AND schema_hash = '{_NEW_SCHEMA_HASH}'
        """
    )
    op.execute(
        f"""
        UPDATE editor_documents
        SET collaboration_schema_version = 1,
            collaboration_schema_hash = '{_OLD_SCHEMA_HASH}'
        WHERE content_mode = 'collaboration'
          AND collaboration_schema_version = 2
          AND collaboration_schema_hash = '{_NEW_SCHEMA_HASH}'
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        DROP CONSTRAINT ck_collaboration_updates_decision_outcome
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        DROP CONSTRAINT ck_collaboration_updates_change_summary
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        DROP COLUMN decision_outcome
        """
    )
    op.execute(
        """
        ALTER TABLE editor_collaboration_updates
        DROP COLUMN change_summary
        """
    )
