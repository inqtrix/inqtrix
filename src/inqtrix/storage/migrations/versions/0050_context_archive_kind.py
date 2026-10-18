"""Kernel context archive artifact kind.

Revision ID: 0050_context_archive
Revises: 0049_tenant_integrity

One additive CHECK widening: ``ck_run_artifacts_kind`` admits
``'context_archive'`` — the kernel's run-local compaction archive
(evicted transcript history plus offloaded bulk tool results, one
session-less artifact per run, ``art_<run12>_ctx``). The durable half of
ledger-grounded compaction: the transcript keeps a digest, the archive
keeps the full text, and the model can re-read it via ``read_canvas``.
"""

from __future__ import annotations

from alembic import op

revision = "0050_context_archive"
down_revision = "0049_tenant_integrity"
branch_labels = None
depends_on = None

_ARTIFACT_KINDS_NEW = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch', "
    "'answer', 'deliverable', 'context_archive'"
)
_ARTIFACT_KINDS_OLD = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch', "
    "'answer', 'deliverable'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE run_artifacts DROP CONSTRAINT IF EXISTS "
        "ck_run_artifacts_kind"
    )
    op.execute(
        "ALTER TABLE run_artifacts ADD CONSTRAINT ck_run_artifacts_kind "
        f"CHECK (kind IN ({_ARTIFACT_KINDS_NEW}))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE run_artifacts DROP CONSTRAINT IF EXISTS "
        "ck_run_artifacts_kind"
    )
    # The old schema cannot represent these rows; deleting them is the
    # honest downgrade (same pattern as 0040).
    op.execute("DELETE FROM run_artifacts WHERE kind = 'context_archive'")
    op.execute(
        "ALTER TABLE run_artifacts ADD CONSTRAINT ck_run_artifacts_kind "
        f"CHECK (kind IN ({_ARTIFACT_KINDS_OLD}))"
    )
