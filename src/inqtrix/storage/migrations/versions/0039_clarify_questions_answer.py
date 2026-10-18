"""Structured clarification rounds + the chat-form answer artifact kind.

Revision ID: 0039_clarify_questions_answer
Revises: 0038_account_memory_optin

This migration adds two related pieces in one revision:

1. ``run_clarifications`` gains the structured gate-round payload —
   ``questions`` (1-3 questions with pickable options, sanitized ids) and
   ``answers`` (the per-question answers map). The legacy
   question/options/answer/option_id columns stay authoritative for
   whole-round free-text answers, so old rows and old clients keep
   working unchanged.
2. ``ck_run_artifacts_kind`` is recreated to admit ``'answer'`` — the
   run-local chat-form deliverable rendered inline in the agent timeline
   and written by the synthesize chat branch.
"""

from __future__ import annotations

from alembic import op

revision = "0039_clarify_questions_answer"
down_revision = "0038_account_memory_optin"
branch_labels = None
depends_on = None

_KINDS_WITH_ANSWER = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch', 'answer'"
)
_KINDS_WITHOUT_ANSWER = (
    "'memo', 'evidence_bundle', 'critic_report', 'editor_patch'"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE run_clarifications "
        "ADD COLUMN IF NOT EXISTS questions json NOT NULL DEFAULT '[]'"
    )
    op.execute(
        "ALTER TABLE run_clarifications "
        "ADD COLUMN IF NOT EXISTS answers json NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE run_artifacts DROP CONSTRAINT IF EXISTS "
        "ck_run_artifacts_kind"
    )
    op.execute(
        "ALTER TABLE run_artifacts ADD CONSTRAINT ck_run_artifacts_kind "
        f"CHECK (kind IN ({_KINDS_WITH_ANSWER}))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE run_artifacts DROP CONSTRAINT IF EXISTS "
        "ck_run_artifacts_kind"
    )
    # The old schema cannot represent 'answer' artifacts; keeping them
    # would make the constraint recreate fail. Removing them here is the
    # deliberate, visible cost of a downgrade.
    op.execute("DELETE FROM run_artifacts WHERE kind = 'answer'")
    op.execute(
        "ALTER TABLE run_artifacts ADD CONSTRAINT ck_run_artifacts_kind "
        f"CHECK (kind IN ({_KINDS_WITHOUT_ANSWER}))"
    )
    op.execute(
        "ALTER TABLE run_clarifications DROP COLUMN IF EXISTS answers"
    )
    op.execute(
        "ALTER TABLE run_clarifications DROP COLUMN IF EXISTS questions"
    )
