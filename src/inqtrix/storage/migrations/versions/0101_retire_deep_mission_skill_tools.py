"""Rewrite stored skill tool lists that name the retired mission tool.

Revision ID: 0101_retire_mission_skill_tools
Revises: 0100_runs_workflow_contract

``run_deep_mission`` delegated an assignment to the mission engine, which is
out of service; the kernel no longer has the tool, and the skill vocabulary
no longer accepts it. A stored skill whose ``allowed_tools`` names it names a
tool that no longer exists; a list with only that name would restrict its runs
to nothing that runs. This revision replaces the
name with ``run_web_research``, the multi-step web research the kernel
delegates instead, in every tenant and once:

* The research takes the place of the mission tool in the list, and every
  tool of a rewritten list stays in it once (a list that already allowed the
  research names it once).
* No list ends up empty, so no skill silently loses its restriction.
* ``skill_templates.revision`` and ``updated_at`` stay: nobody edited the
  skill, and runs bound to its revision keep reading it.

It logs how many skills it rewrote, as a WARNING when there were any.
Without such skills the revision changes nothing.

The downgrade changes nothing: which lists named the mission tool is not
recorded, and the rewritten lists are valid for the previous revision too.
Back up the database before the upgrade.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0101_retire_mission_skill_tools"
down_revision = "0100_runs_workflow_contract"
branch_labels = None
depends_on = None

# Alembic loads this file under another name; the import path keeps the
# records under ``inqtrix``, where the log filter shows them.
log = logging.getLogger("inqtrix.storage.migrations.versions.0101_retire_deep_mission_skill_tools")

_REWRITE_SQL = """
UPDATE skill_templates AS skill
SET allowed_tools = rewritten.tools
FROM (
    SELECT mapped.id, jsonb_agg(mapped.tool ORDER BY mapped.first_position) AS tools
    FROM (
        SELECT
            source.id,
            CASE
                WHEN element.value = to_jsonb('run_deep_mission'::text)
                    THEN to_jsonb('run_web_research'::text)
                ELSE element.value
            END AS tool,
            min(element.position) AS first_position
        FROM skill_templates AS source
        CROSS JOIN LATERAL jsonb_array_elements(source.allowed_tools)
            WITH ORDINALITY AS element(value, position)
        WHERE jsonb_typeof(source.allowed_tools) = 'array'
          AND source.allowed_tools @> to_jsonb(ARRAY['run_deep_mission'])
        GROUP BY source.id, 2
    ) AS mapped
    GROUP BY mapped.id
) AS rewritten
WHERE skill.id = rewritten.id
"""


def upgrade() -> None:
    rewritten = int(op.get_bind().execute(sa.text(_REWRITE_SQL)).rowcount or 0)
    if rewritten:
        log.warning(
            "Skills whose tool list named the retired run_deep_mission now name "
            "run_web_research instead: %d.",
            rewritten,
        )
    else:
        log.info("Skills whose tool list named the retired run_deep_mission: 0.")


def downgrade() -> None:
    """Nothing to undo: the rewritten lists are valid for the previous revision."""
