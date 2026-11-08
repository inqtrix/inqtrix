"""The event types of a run, in one place: every emitter imports them from here."""

from __future__ import annotations

# Run lifecycle
RUN_QUEUED_EVENT = "inqtrix.run.queued"
RUN_STARTED_EVENT = "inqtrix.run.started"
RUN_WAITING_EVENT = "inqtrix.run.waiting"
RUN_RESUMED_EVENT = "inqtrix.run.resumed"
RUN_CANCEL_REQUESTED_EVENT = "inqtrix.run.cancel_requested"
RUN_CANCELLED_EVENT = "inqtrix.run.cancelled"
RUN_COMPLETED_EVENT = "inqtrix.run.completed"
RUN_FAILED_EVENT = "inqtrix.run.failed"
RUN_TRACE_EVENT = "inqtrix.run.trace"
SNAPSHOT_EVENT = "inqtrix.run.snapshot"
STREAM_LIVE_EVENT = "inqtrix.stream.live"

# Answer
ANSWER_STARTED_EVENT = "inqtrix.answer.started"
ANSWER_READY_EVENT = "inqtrix.answer.ready"
ANSWER_INTERRUPTED_EVENT = "inqtrix.answer.interrupted"
ANSWER_DRAFT_EVENT = "inqtrix.answer.draft"
"""The answer a reviewed run shows while its review runs: the draft after its
citation check, whole, once before the review starts. The published answer
replaces it when it is ready; the draft keeps its own text."""

# Research
RESEARCH_QUERY_STARTED_EVENT = "inqtrix.research.query.started"
RESEARCH_QUERY_FINISHED_EVENT = "inqtrix.research.query.finished"

# Agent
PHASE_CHANGED_EVENT = "inqtrix.agent.phase.changed"
TODO_UPDATED_EVENT = "inqtrix.agent.todo.updated"
ACTIVITY_EVENT = "inqtrix.agent.activity"
NARRATION_EVENT = "inqtrix.agent.narration"
PATCH_PROPOSED_EVENT = "inqtrix.agent.patch.proposed"
PLAN_PROPOSED_EVENT = "inqtrix.agent.plan.proposed"
PLAN_REVISED_EVENT = "inqtrix.agent.plan.revised"
TASK_STARTED_EVENT = "inqtrix.agent.task.started"
TASK_FINISHED_EVENT = "inqtrix.agent.task.finished"
TASK_FAILED_EVENT = "inqtrix.agent.task.failed"
TASK_CANCEL_REQUESTED_EVENT = "inqtrix.agent.task.cancel_requested"
CHILD_PROGRESS_EVENT = "inqtrix.agent.child.progress"
CHILD_BATCH_GUARDED_EVENT = "inqtrix.agent.child_batch.guarded"
OUTCOME_EVENT = "inqtrix.agent.outcome"
"""An agent run's outcome record: how its review ended and what stayed open;
only a delegated mission's crosses to its parent, as numbers and its bounded
summary sentence."""

# Tools
TOOL_STARTED_EVENT = "inqtrix.agent.tool.started"
TOOL_FINISHED_EVENT = "inqtrix.agent.tool.finished"
"""The two events of one tool row in a run's timeline, joined by their
``invocation_id``. A delegation row uses the child's origin key as that id. The
finished one is the one tool event that carries the server's call counter (its
snapshot) and the call's own position."""
TOOL_LIMIT_REACHED_EVENT = "inqtrix.agent.tool_limit.reached"
SKILL_LOADED_EVENT = "inqtrix.agent.skill.loaded"

# Interaction
APPROVAL_REQUESTED_EVENT = "inqtrix.agent.approval.requested"
APPROVAL_DECIDED_EVENT = "inqtrix.agent.approval.decided"
CLARIFICATION_REQUESTED_EVENT = "inqtrix.agent.clarification.requested"
CLARIFICATION_ANSWERED_EVENT = "inqtrix.agent.clarification.answered"
LIMIT_REACHED_EVENT = "inqtrix.agent.limit.reached"
"""Audit/event signal emitted when a run parks at a recoverable limit."""
LIMIT_DECIDED_EVENT = "inqtrix.agent.limit.decided"
"""Audit signal emitted when a terminal partial/cancel choice is applied.

Extensions are already durably visible through the canonical
``clarification.answered`` event and row. Emitting a second apply event on
every checkpoint redelivery would make an exactly-once claim the event stream
cannot guarantee.
"""

# Artifacts
ARTIFACT_CREATED_EVENT = "inqtrix.agent.artifact.created"
ARTIFACT_UPDATED_EVENT = "inqtrix.agent.artifact.updated"
ARTIFACT_EDIT_CONFLICT_EVENT = "inqtrix.agent.artifact.edit_conflict"

# Context
CANVAS_CONTEXT_ATTACHED_EVENT = "inqtrix.agent.canvas_context.attached"
"""The durable record of WHICH canvas comments rode a submission: run summaries
deliberately never carry the canvas context, so the transcript surfaces it from
this replayable event."""
REQUIREMENT_ATTACHED_EVENT = "inqtrix.agent.requirement.attached"
"""The durable record of the result requirement a run starts under: which library
rules at which frozen revision, how long the typed half is and whether a review
pass checks the result. Emitted once, when the first segment seeds the run (kernel
and mission alike)."""
CONTEXT_COMPACTED_EVENT = "inqtrix.agent.context.compacted"

# Model and judgement
MODEL_RECOVERY_EVENT = "inqtrix.agent.model.recovery"
SUFFICIENCY_JUDGED_EVENT = "inqtrix.agent.sufficiency.judged"
"""Event type of one advisory sufficiency verdict (nudge or fallback)."""
CITATION_VALIDATION_EVENT = "inqtrix.agent.citation.validation"
