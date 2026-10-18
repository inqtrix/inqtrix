"""Kernel control-store kinds: tool approvals + deliverable artifacts.

Revision ID: 0040_kernel_control_kinds
Revises: 0039_clarify_questions_answer

Two additive CHECK widenings support the cognitive kernel:

1. ``ck_run_approvals_kind`` admits ``'tool'`` — the kernel's per-call
   policy gate whose payload carries the proposed actions (web query
   verbatim in the args).
2. ``ck_run_artifacts_kind`` admits ``'deliverable'`` — kernel canvas
   documents written via ``write_canvas``; several per session are
   distinguished through the artifact registry.
"""

from __future__ import annotations

from alembic import op

revision = "0040_kernel_control_kinds"
down_revision = "0039_clarify_questions_answer"
branch_labels = None
depends_on = None

_APPROVAL_KINDS_NEW = "'discovery', 'plan', 'patch', 'replan', 'tool'"
_APPROVAL_KINDS_OLD = "'discovery', 'plan', 'patch', 'replan'"
_ARTIFACT_KINDS_NEW = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch', "
    "'answer', 'deliverable'"
)
_ARTIFACT_KINDS_OLD = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch', 'answer'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE run_approvals DROP CONSTRAINT IF EXISTS "
        "ck_run_approvals_kind"
    )
    op.execute(
        "ALTER TABLE run_approvals ADD CONSTRAINT ck_run_approvals_kind "
        f"CHECK (kind IN ({_APPROVAL_KINDS_NEW}))"
    )
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
    # deliberate, visible cost of a downgrade (same rule as 0039).
    op.execute("DELETE FROM run_artifacts WHERE kind = 'deliverable'")
    op.execute(
        "ALTER TABLE run_artifacts ADD CONSTRAINT ck_run_artifacts_kind "
        f"CHECK (kind IN ({_ARTIFACT_KINDS_OLD}))"
    )
    op.execute(
        "ALTER TABLE run_approvals DROP CONSTRAINT IF EXISTS "
        "ck_run_approvals_kind"
    )
    op.execute("DELETE FROM run_approvals WHERE kind = 'tool'")
    op.execute(
        "ALTER TABLE run_approvals ADD CONSTRAINT ck_run_approvals_kind "
        f"CHECK (kind IN ({_APPROVAL_KINDS_OLD}))"
    )
