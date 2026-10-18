"""Orchestration for the agent control surface (plans, approvals,
clarifications, artifacts).

Sits between the thin routers in
:mod:`inqtrix.server.routers.agent_runs` and the store pair behind
:class:`~inqtrix.agents.control_ports.AgentControlStore`. Responsibilities:

* compose interrupt resolutions with the RUN STORE so the decision write
  and the ``waiting -> queued`` flip stay atomic per backend (the store's
  ``*_and_resume`` methods own the mechanics, this service supplies the
  resume callable);
* validate edit decisions through THE plan schema/validator
  (:mod:`inqtrix.agents.plan_models` / ``plan_validation`` — the same
  rules the planner runs against);
* emit the agent event signals (decision/answer signals BEFORE the
  resume while the run is still parked — never droppable by the
  ends-terminal log guard; artifact-edit signals after their commit)
  and the audit rows after success. Throughout: events are signals,
  rows are truth — a failed signal is logged loudly, never rolled into
  a transaction.

The router performs the first indistinguishable-404 gate. The service carries
the same live user context into every subsequent run read and mutation so a
revocation between routing and the control write cannot revive stale access.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from inqtrix.agents.report_requirement import split_composed_requirement
from inqtrix.constants import MAX_PLAN_TASKS_DEFAULT
from inqtrix.services.report_requirement_resolver import (
    ReportRequirementError,
    distinct_rule_ids,
    resolve_report_requirement,
)
from inqtrix.agents.control_ports import (
    APPROVAL_DECISIONS,
    APPROVAL_STATUS_BY_DECISION,
    ApprovalAlreadyDecided,
    ApprovalRecord,
    ArtifactRecord,
    ArtifactRevisionRecord,
    ClarificationAlreadyAnswered,
    ClarificationRecord,
    PlanNotFound,
    PlanRecord,
    PlanTaskNotFound,
    PlanTaskRecord,
    additive_replan_errors,
    carry_forward_terminal_task_results,
    decided_requirement_parts,
    settle_cancelled_plan_tasks,
    settle_terminal_plan_tasks,
)
from inqtrix.agents.plan_collections import (
    CollectionCatalogEntry,
    resolve_plan_collections,
)
from inqtrix.agents.intake import without_stale_web_note
from inqtrix.agents.plan_models import ExecutionPlanModel
from inqtrix.agents.plan_validation import validate_plan
from inqtrix.agents.scheduler import (
    project_child_run_outcome,
    task_result_payload,
    task_result_summary,
)
from inqtrix.agents.report_quality import (
    guarded_reader_text,
    writer_identifiers,
)
from inqtrix.agents.web_execution_policy import derive_web_research_policy
from inqtrix.auth.permissions import AuditEntry
from inqtrix.exceptions import RunNotFound
from inqtrix.i18n import t
from inqtrix.execution_authority import pinned_knowledge_collection_ids
from inqtrix.prompt_language import resolve_answer_language
from inqtrix.runs.shared import TERMINAL_RUN_STATUS_VALUES

if TYPE_CHECKING:
    from inqtrix.services.prompt_template_service import (
        PromptTemplateService,
    )
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.runs.ports import RunStorePort
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )


class CollectionPreflight(Protocol):
    """Collection visibility, narrowed to what the approval-edit path
    needs — satisfied by ``KnowledgeService``.

    Kept a Protocol so this service depends on the lookup, not the whole
    knowledge service, and stays trivially fakeable in tests. The edit
    path builds the caller-visible CATALOG from this (the same
    per-collection access rule the retrieval gate enforces) and runs THE
    shared resolver + validator against it, so agent plans and user edits can
    never drift apart.
    """

    async def list_collections(
        self,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[Any]:
        ...

log = logging.getLogger("inqtrix")

APPROVAL_DECIDED_EVENT = "inqtrix.agent.approval.decided"
CLARIFICATION_ANSWERED_EVENT = "inqtrix.agent.clarification.answered"
ARTIFACT_UPDATED_EVENT = "inqtrix.agent.artifact.updated"
TASK_CANCEL_REQUESTED_EVENT = "inqtrix.agent.task.cancel_requested"

class AgentControlValidationError(ValueError):
    """Invalid decision/answer/edit input (HTTP 400).

    Attributes:
        errors: Every collected violation (German, user-facing) — the plan
            validator reports ALL problems at once so the user fixes the
            edit in one round.
    """

    def __init__(
        self,
        errors: list[str],
        *,
        error_type: str = "invalid_request_error",
    ) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.error_type = error_type


class AgentControlUnavailable(RuntimeError):
    """A required collaborator is not wired (HTTP 502, loud)."""


def _same_decision(
    stored: ApprovalRecord, decision: str, decision_payload: dict[str, Any]
) -> bool:
    """Replay identity: verb AND payload (the edited plan) must match."""
    return (
        stored.decision == decision
        and dict(stored.decision_payload) == decision_payload
    )


def _stored_requirement(
    stored: ApprovalRecord,
    report_guidance: str | None,
    report_rule_ids: list[str] | None,
) -> dict[str, Any] | None:
    """The requirement a decided gate stored, when a request names it again.

    The same requirement is the same typed text and the same distinct rule
    ids in the same order, read against the parts the decision kept; the
    result is the stored ``report_guidance`` and ``report_requirement``,
    so the replay answers with exactly what was decided. ``None`` when the
    request names another requirement, or the decision stored none.
    """
    parts = stored.decision_payload.get("report_requirement")
    if not isinstance(parts, dict):
        return None
    same = parts.get("free_text") == (report_guidance or "") and [
        str(rule.get("template_id")) for rule in parts.get("rules") or []
    ] == distinct_rule_ids(report_rule_ids or [])
    if not same:
        return None
    return {
        "report_guidance": stored.decision_payload["report_guidance"],
        "report_requirement": parts,
    }


def _validated_tool_edit(
    approval: ApprovalRecord, actions_body: list[Any] | None
) -> list[dict[str, Any]]:
    """The normalized edited action list for a tool gate, or a loud 400.

    A tool approval is editable only when it proposes EXACTLY one action
    (the HITL ``edited_action`` resume contract carries one action per
    decision); the edit replaces the args, never the tool — swapping the
    tool would grant something the gate never showed the user.
    """
    proposed = approval.payload.get("actions")
    if (
        not isinstance(proposed, list)
        or len(proposed) != 1
        or not isinstance(proposed[0], dict)
    ):
        raise AgentControlValidationError(
            [
                ("This tool approval cannot be edited because it does not contain "
                "exactly one action.")
            ]
        )
    proposed_tool = str(proposed[0].get("tool", ""))
    if not isinstance(actions_body, list) or len(actions_body) != 1:
        raise AgentControlValidationError(
            [
                "An edit decision requires an actions body with exactly one action."
            ]
        )
    action = actions_body[0]
    if not isinstance(action, dict) or not isinstance(
        action.get("args"), dict
    ):
        raise AgentControlValidationError(
            ["Each action requires an args object."]
        )
    edited_tool = action.get("tool", proposed_tool)
    if edited_tool != proposed_tool:
        raise AgentControlValidationError(
            [
                f'The tool cannot be changed (expected {proposed_tool!r}, received {edited_tool!r}).'
            ]
        )
    for identifier_key in ("action_id", "id"):
        proposed_id = proposed[0].get(identifier_key)
        edited_id = action.get(identifier_key, proposed_id)
        if proposed_id is not None and edited_id != proposed_id:
            raise AgentControlValidationError(
                ["The stored action ID cannot be changed."]
            )

    from inqtrix.agents.kernel.tools import build_kernel_tools

    tools = {
        str(tool.name): tool
        for tool in build_kernel_tools()
    }
    selected = tools.get(proposed_tool)
    if selected is None:
        raise AgentControlValidationError(
            [f'Unknown tool {proposed_tool!r}.']
        )
    args = dict(action["args"])
    schema = selected.tool_call_schema
    unknown = sorted(set(args) - set(schema.model_fields))
    if unknown:
        raise AgentControlValidationError(
            ["Unknown tool fields: " + ", ".join(unknown)]
        )
    try:
        schema.model_validate(args, strict=True)
    except ValidationError as exc:
        errors = [
            "Tool arguments do not match the input schema: "
            + "; ".join(
                ".".join(str(part) for part in item.get("loc", ()))
                for item in exc.errors()
            )
        ]
        raise AgentControlValidationError(errors) from exc
    normalized = {"tool": proposed_tool, "args": args}
    for identifier_key in ("action_id", "id"):
        if identifier_key in proposed[0]:
            normalized[identifier_key] = proposed[0][identifier_key]
    return [normalized]


def _validated_round_answers(
    clarification: ClarificationRecord, answers: dict[str, Any]
) -> dict[str, Any]:
    """The normalized structured answers map, or a loud 400.

    The round parks the run exactly once, so EVERY question must be
    resolved in one submission — a partial map would strand the
    remaining questions with no second gate to ask them.
    """
    questions = {
        str(question.get("id", "")): question
        for question in clarification.questions
    }
    if not questions:
        raise AgentControlValidationError(
            ["This clarification has no structured questions; use answer or option_id."]
        )
    errors: list[str] = []
    normalized: dict[str, Any] = {}
    for question_id, raw in answers.items():
        question = questions.get(str(question_id))
        if question is None:
            errors.append(f'Unknown question {question_id!r}.')
            continue
        entry = raw if isinstance(raw, dict) else {}
        option_ids = entry.get("option_ids") or []
        text = str(entry.get("text", "") or "").strip()
        if not isinstance(option_ids, list) or any(
            not isinstance(oid, str) for oid in option_ids
        ):
            errors.append(
                f'Question {question_id!r}: option_ids must be a list of strings.'
            )
            continue
        known = {
            str(option.get("id", ""))
            for option in question.get("options", [])
        }
        unknown = [oid for oid in option_ids if oid not in known]
        if unknown:
            errors.append(
                f"""Question {question_id!r}: unknown option(s) {', '.join(unknown)}."""
            )
        if len(option_ids) > 1 and not question.get("multi_select"):
            errors.append(
                f'Question {question_id!r} allows only one option.'
            )
        if not option_ids and not text:
            errors.append(
                f'Question {question_id!r}: select option(s) or provide free text.'
            )
        normalized[str(question_id)] = {
            "option_ids": list(dict.fromkeys(option_ids)),
            "text": text,
        }
    missing = sorted(set(questions) - set(normalized))
    if missing:
        errors.append(
            f"""All questions in the round must be answered; missing: {', '.join(missing)}."""
        )
    if errors:
        raise AgentControlValidationError(errors)
    return normalized


class AgentControlService:
    """Agent control orchestration over one store pair + the run store.

    Args:
        store: The control store (memory or Postgres, one port).
        run_store: The run store the interrupt resolutions resume through.
        audit: Optional audit sink; ``None`` skips audit rows (memory/dev
            deployments without identity persistence).
        editor_persistence: Optional editor service backing artifact
            export; ``None`` makes export fail loudly with 502.
        durable: Whether the store pair is Postgres-backed — surfaced to
            capability consumers, never used to branch behavior here (the
            stores own their backend differences).
    """

    def __init__(
        self,
        *,
        store: "AgentControlStore",
        run_store: "RunStorePort",
        audit: Any = None,
        editor_persistence: "EditorPersistenceService | None" = None,
        knowledge: "CollectionPreflight | None" = None,
        prompt_templates: "PromptTemplateService | None" = None,
        durable: bool = False,
        max_plan_tasks: int = MAX_PLAN_TASKS_DEFAULT,
    ) -> None:
        self._store = store
        self._run_store = run_store
        self._audit = audit
        self._editor_persistence = editor_persistence
        self._knowledge = knowledge
        self._prompt_templates = prompt_templates
        self._durable = durable
        self._max_plan_tasks = max_plan_tasks

    @property
    def store(self) -> "AgentControlStore":
        """The underlying control store (the agent runtime's write surface)."""
        return self._store

    @property
    def durable(self) -> bool:
        """Whether control rows survive restarts (Postgres backend)."""
        return self._durable

    @staticmethod
    def _caller_context(
        principal: "Principal | None",
        visible_to: "UserContext | None" = None,
    ) -> "UserContext | None":
        """Return the live canonical-user context for a control command."""
        if visible_to is not None or principal is None:
            return visible_to
        if principal.user_id is None:
            return None
        from inqtrix.auth.principal import UserContext

        return UserContext(principal=principal)

    async def _execution_context(self, run_id: str) -> "UserContext | None":
        """Resolve the current effective actor for internal reconciliation."""
        principal = await asyncio.to_thread(
            self._run_store.execution_principal, run_id
        )
        return self._caller_context(principal)

    async def _run_summary(
        self,
        run_id: str,
        *,
        principal: "Principal | None" = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Read a run through the caller or its current effective actor."""
        context = self._caller_context(principal, visible_to)
        if context is None and principal is None:
            context = await self._execution_context(run_id)
        return await asyncio.to_thread(
            self._run_store.get, run_id, visible_to=context
        )

    # -- plans ------------------------------------------------------------ #

    async def get_plan(
        self, run_id: str, *, version: int | None = None
    ) -> tuple[PlanRecord, list[PlanTaskRecord], list[PlanRecord]]:
        """One plan version (latest by default), its tasks, all versions."""
        plan, tasks = await self._store.get_plan(run_id, version=version)
        if await self.reconcile_terminal_tasks(run_id) and version is None:
            plan, tasks = await self._store.get_plan(run_id)
        versions = await self._store.list_plan_versions(run_id)
        return plan, tasks, versions

    async def get_task_result(
        self,
        run_id: str,
        task_id: str,
        *,
        version: int | None = None,
    ) -> PlanTaskRecord:
        """Return one task whose complete result is loaded on demand.

        The plan response intentionally remains compact. This lookup resolves
        the same authoritative plan version and returns the task row whose
        internal payload contains complete answer Markdown and evidence.
        """
        _plan, tasks, _versions = await self.get_plan(run_id, version=version)
        try:
            return next(task for task in tasks if task.task_id == task_id)
        except StopIteration as exc:
            raise PlanTaskNotFound(task_id) from exc

    async def request_task_cancel(
        self,
        run_id: str,
        task_id: str,
        *,
        workspace_id: str | None,
        principal: "Principal | None",
        visible_to: "UserContext | None" = None,
    ) -> PlanTaskRecord:
        """Cancel one source task without cancelling its parent run.

        The task-row CAS and an optional research-child cancellation share the
        run store's live authorization transaction. A synchronous local task
        remains ``cancel_requested`` until its current provider call returns
        and the mission discards that result.
        """
        plan, tasks = await self._store.get_plan(run_id)
        if not any(row.task_id == task_id for row in tasks):
            raise PlanTaskNotFound(task_id)
        stored, child_status = await self._store.request_plan_task_cancel(
            run_id=run_id,
            plan_id=plan.plan_id,
            task_id=task_id,
            authorize=self._authorized_control_callable(
                run_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            ),
        )
        await self._emit(
            run_id,
            TASK_CANCEL_REQUESTED_EVENT,
            {
                "task_id": stored.task_id,
                "status": stored.status,
                "child_run_id": stored.child_run_id,
                **(
                    {"child_run_status": child_status}
                    if child_status is not None
                    else {}
                ),
            },
        )
        return stored

    async def reconcile_terminal_tasks(self, run_id: str) -> bool:
        """Repair unfinished control rows for a terminal agent run.

        Run terminal state and plan rows live in separate ports. Normal graph
        unwinding closes its own tasks, while immediate queue/wait cancellation,
        worker failure, or a process crash can prevent that code from running.
        This idempotent read-side recovery keeps reloads authoritative without
        adding another event consumer or persistence layer.

        Returns:
            ``True`` when the run is cancelled/failed and reconciliation was
            attempted, otherwise ``False``.
        """
        try:
            summary = await self._run_summary(run_id)
        except RunNotFound:
            return False
        status = str(summary.get("status") or "")
        if status not in ("cancelled", "failed"):
            return False
        if status == "cancelled":
            await self.settle_cancelled_tasks(run_id)
        else:
            await settle_terminal_plan_tasks(
                self._store, run_id, status="failed"
            )
        released, settled = await self._store.settle_terminal_control_rows(
            run_id
        )
        if released or settled:
            log.info(
                "Terminal run %s: released %d artifacts and closed %d approvals.",
                run_id,
                released,
                settled,
            )
        return True

    async def reconcile_terminal_run_tree(
        self, run_ids: tuple[str, ...]
    ) -> None:
        """Reconcile every run affected by one store-level tree cancel."""
        for run_id in run_ids:
            await self.reconcile_terminal_tasks(run_id)

    async def settle_cancelled_tasks(self, run_id: str) -> None:
        """Fold terminal children before closing a cancelled parent plan.

        A waiting or queued parent becomes terminal inside the run store and
        never re-enters its graph. Its child may already have completed in the
        narrow wake/cancel race, so the HTTP path uses the same pure child
        projector as the graph before the generic pending/running settlement.
        """
        try:
            _plan, tasks = await self._store.get_plan(run_id)
        except PlanNotFound:
            return
        for task in tasks:
            if task.status != "running" or not task.child_run_id:
                continue
            try:
                child_context = await self._execution_context(
                    task.child_run_id
                )
                child = await asyncio.to_thread(
                    self._run_store.get,
                    task.child_run_id,
                    visible_to=child_context,
                )
            except RunNotFound:
                child = {}
            attempt = max(1, int(child.get("parent_task_attempt", 1) or 1))
            outcome = await asyncio.to_thread(
                project_child_run_outcome,
                self._run_store,
                task.child_run_id,
                attempt,
                visible_to=child_context,
            )
            if outcome is None:
                continue
            # The same reader surface the run writes itself, reached on
            # the recovery path: this row is what the task card shows
            # after a parent died mid-flight, so it passes the same
            # guard. Bound to what is available HERE - the child's own
            # ledger, and no user text: the parent's words are not in
            # reach, and exempting the child's question instead would
            # exempt a planner-written objective, which is the opposite
            # of what the exemption is for. A user's own identifier is
            # masked on this path and stays visible as a marker; that is
            # an over-repair on a recovery row, not a leak.
            summary = task_result_summary(
                guarded_reader_text(
                    outcome.summary or outcome.failure_reason,
                    writer_identifiers(list(outcome.evidence)),
                    authored_by="",
                    marker=t(
                        {
                            "language": resolve_answer_language(
                                str(child.get("question") or "")
                            ).code
                        },
                        "agent_reader_identifier_removed",
                    ),
                )
            )
            await self._store.transition_plan_task(
                run_id=task.run_id,
                plan_id=task.plan_id,
                task_id=task.task_id,
                status=outcome.status,
                child_run_id=outcome.child_run_id,
                result_summary=summary,
                result_payload=task_result_payload(
                    outcome, persisted_summary=summary
                ),
            )
        await settle_cancelled_plan_tasks(self._store, run_id)

    # -- approvals --------------------------------------------------------- #

    async def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        return await self._store.list_approvals(run_id)

    async def decide_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        plan_body: dict[str, Any] | None,
        note: str,
        report_guidance: str | None = None,
        report_rule_ids: list[str] | None = None,
        principal: "Principal | None" = None,
        visible_to: "UserContext | None" = None,
        actions_body: list[Any] | None = None,
        approval_scope: str = "",
    ) -> tuple[ApprovalRecord, dict[str, Any], bool]:
        """Resolve one approval; returns (approval, run summary, replayed).

        ``replayed`` is True when the SAME decision was already recorded —
        the idempotent 200 path (no second resume, no second event).

        ``visible_to`` is the deciding caller's context: an edited plan's
        collection references run through THE shared resolver + validator
        (name -> id canonicalization, allowed-ids check against the
        caller-visible catalog), so an invisible or unknown collection is
        a 400 at edit time, not a mid-run task failure (the same
        collection-visibility rule the planner enforces).

        ``actions_body`` is the tool-gate counterpart of ``plan_body``:
        editing a ``kind="tool"`` approval carries the revised
        action list — exactly one action whose args replace the proposed
        ones; the tool itself is not swappable through an edit.

        ``approval_scope``: ``"run"`` on a plain tool-gate approve
        stores a run-wide grant in ``decision_payload`` — the gated tools
        of this gate stop gating for the rest of the run (folded at every
        segment start; effective only in the balanced mode table, strict
        stays per-call). ``"once"``/empty stores nothing, so the replay
        identity of a plain approve is unchanged. Because
        ``decision_payload`` IS replay identity, an approve-once retry
        after an approve-run (or vice versa) conflicts with 409 — exactly
        like a retry carrying a different edited plan.
        ``ALWAYS_GATED_TOOLS`` are never grantable.

        Raises:
            AgentControlValidationError: Unknown decision verb, edit
                without/with-invalid body, edit on a kind that does not
                support it, or an edited rag task referencing an
                invisible collection.
            ApprovalNotFound / ApprovalAlreadyDecided / RunNotFound /
            RunActive: Mapped by the router (404 / 409).
        """
        visible_to = self._caller_context(principal, visible_to)
        if decision not in APPROVAL_DECISIONS:
            raise AgentControlValidationError(
                [
                    f"""Unknown decision {decision!r} (allowed: {', '.join(APPROVAL_DECISIONS)})."""
                ]
            )
        approval = await self._store.get_approval(run_id, approval_id)
        edited_plan: PlanRecord | None = None
        edited_tasks: list[PlanTaskRecord] = []
        decision_payload: dict[str, Any] = {}
        if decision == "edit":
            if approval.kind == "tool":
                if plan_body is not None:
                    raise AgentControlValidationError(
                        [
                            "Edit a tool approval using an actions body, not a plan."
                        ]
                    )
                decision_payload = {
                    "actions": _validated_tool_edit(approval, actions_body)
                }
            elif approval.kind in ("plan", "replan"):
                if actions_body is not None:
                    raise AgentControlValidationError(
                        [
                            "An actions body is allowed only for tool approvals."
                        ]
                    )
                if not isinstance(plan_body, dict):
                    raise AgentControlValidationError(
                        ["An edit decision requires a plan body."]
                    )
                edited_plan, edited_tasks = await self._parse_edited_plan(
                    run_id, plan_body, visible_to
                )
                decision_payload = {"plan": plan_body}
            else:
                raise AgentControlValidationError(
                    [
                        f'Only plan and tool approvals can be decided with changes (kind={approval.kind}).'
                    ]
                )
        elif plan_body is not None:
            raise AgentControlValidationError(
                ["A plan body is allowed only for decision=edit."]
            )
        elif actions_body is not None:
            raise AgentControlValidationError(
                ["An actions body is allowed only for decision=edit."]
            )
        if (
            report_guidance is not None or report_rule_ids is not None
        ) and approval.kind not in (
            "plan",
            "replan",
        ):
            # Guidance on a gate that cannot honor it must fail loudly —
            # silently dropping user requirements is the one thing the
            # report_guidance feature exists to prevent.
            raise AgentControlValidationError(
                [
                    f'report_guidance/report_rule_ids are allowed only for plan/replan approvals (this gate: {approval.kind}).'
                ]
            )
        if (
            report_guidance is not None or report_rule_ids is not None
        ) and approval.status != "pending":
            # Replay identity of a decided gate is the REQUEST, compared
            # before anything is resolved: the decision stored the rules
            # it added at the revision the catalog had then. Resolving
            # again would read a template edited or deleted since and turn
            # the replay of the same decision into a conflict, or into a
            # 400 for a rule that is no longer visible.
            stored_requirement = _stored_requirement(
                approval, report_guidance, report_rule_ids
            )
            if stored_requirement is None:
                raise ApprovalAlreadyDecided(approval)
            decision_payload = {**decision_payload, **stored_requirement}
        elif report_guidance is not None or report_rule_ids is not None:
            # Decision-scoped result requirement rides the SAME payload
            # the edit decision already uses — no schema change; the
            # resume hands it to the synthesis prompts.
            # PRESENCE, not truthiness: an empty value is a real one
            # ("drop my earlier requirement"). Storing only non-empty
            # text made a once-set requirement unremovable — the cleared
            # field vanished on the way and the old text kept shaping
            # every later revision.
            # SAME resolution as the submit-time requirement: one
            # catalog lookup, one composition, one ceiling. Two copies
            # would drift, and a drift here is a security drift.
            # A rule the run already works under stays the text and the
            # revision it is frozen under; only a rule the selection adds
            # reads the catalog. Otherwise a change of the typed text alone
            # would pick up a later edit of a rule, or fail on one deleted
            # since.
            frozen = (
                await self._rules_in_force(run_id, approval)
                if report_rule_ids
                else {}
            )
            try:
                composed, rule_parts = await resolve_report_requirement(
                    free_text=report_guidance or "",
                    template_ids=report_rule_ids or [],
                    prompt_templates=self._prompt_templates,
                    visible_to=visible_to,
                    frozen_rules=frozen,
                )
            except ReportRequirementError as exc:
                raise AgentControlValidationError(exc.messages) from exc
            decision_payload = {
                **decision_payload,
                "report_guidance": composed,
                # The parts, for the read-back: the composed text is
                # what the model sees, this is what the user chose.
                "report_requirement": {
                    "free_text": report_guidance or "",
                    "rules": rule_parts,
                },
            }
        if approval_scope:
            if approval_scope not in ("once", "run"):
                raise AgentControlValidationError(
                    [
                        f'Unknown approval_scope {approval_scope!r} (allowed: once, run).'
                    ]
                )
            if decision != "approve":
                raise AgentControlValidationError(
                    ["approval_scope is allowed only for decision=approve."]
                )
            if approval.kind != "tool":
                raise AgentControlValidationError(
                    [
                        f'approval_scope is allowed only for tool approvals (this gate: {approval.kind}).'
                    ]
                )
            if approval_scope == "run":
                from inqtrix.agents.kernel.policy import ALWAYS_GATED_TOOLS

                always_gated = sorted(
                    {
                        str(action.get("tool") or "")
                        for action in approval.payload.get("actions") or []
                    }
                    & set(ALWAYS_GATED_TOOLS)
                )
                if always_gated:
                    raise AgentControlValidationError(
                        [
                            f"""{', '.join(always_gated)} requires approval for each call in every mode; approval_scope=run is not allowed here."""
                        ]
                    )
                # The grant rides decision_payload and thereby BECOMES
                # replay identity; "once" deliberately stores nothing.
                decision_payload = {
                    **decision_payload,
                    "approval_scope": "run",
                }

        decided_by = principal.user_id if principal is not None else None
        if approval.status != "pending":
            # Sequential replay: the SAME decision answers 200 with the
            # stored state, a different one conflicts. For edit the plan
            # payload IS the decision — a retry carrying a DIFFERENT
            # plan must conflict, never be swallowed as a replay.
            if _same_decision(approval, decision, decision_payload):
                summary = await self._run_summary(
                    run_id, principal=principal, visible_to=visible_to
                )
                return approval, summary, True
            raise ApprovalAlreadyDecided(approval)
        replayed = False
        try:
            approval, summary = await self._store.decide_approval_and_resume(
                run_id=run_id,
                approval_id=approval_id,
                decision=decision,
                decision_payload=decision_payload,
                note=note,
                decided_by_user_id=decided_by,
                resume=self._resume_callable(run_id, principal),
                edited_plan=edited_plan,
                edited_tasks=edited_tasks,
            )
        except Exception as exc:
            # Concurrent race: a parallel request may have decided AND
            # resumed between our pre-read and the composed call — the
            # loser surfaces as AlreadyDecided (memory CAS) or RunActive
            # (durable resume CAS fires before the decision writer).
            # Re-read: same decision -> replay 200, else the original
            # error stands.
            replay = await self._approval_replay(
                exc, run_id, approval_id, decision, decision_payload
            )
            if replay is None:
                raise
            approval = replay
            summary = await self._run_summary(
                run_id, principal=principal, visible_to=visible_to
            )
            replayed = True
        if not replayed:
            # Row truth and run resume committed together before this signal.
            # A very fast resumed run may already be terminal and reject the
            # event; that is preferable to a durable false decision signal
            # when the composed transaction rolls back.
            await self._emit(
                run_id,
                APPROVAL_DECIDED_EVENT,
                {
                    "approval_id": approval_id,
                    "status": APPROVAL_STATUS_BY_DECISION[decision],
                    # Event payloads are JSON values.  The durable approval
                    # row keeps its UUID type; the wire event uses the same
                    # string representation as the HTTP contract.
                    "decided_by_user_id": (
                        str(decided_by) if decided_by is not None else None
                    ),
                },
            )
            await self._record_audit(
                principal,
                action="agent.approval_decided",
                run_id=run_id,
                detail={
                    "approval_id": approval_id,
                    "kind": approval.kind,
                    "decision": decision,
                },
            )
        return approval, summary, replayed

    async def _approval_replay(
        self,
        exc: Exception,
        run_id: str,
        approval_id: str,
        decision: str,
        decision_payload: dict[str, Any],
    ) -> ApprovalRecord | None:
        # Function-local import: the services layer must not pull in
        # server modules at import time (layering regression test).
        from inqtrix.server.runs import RunActive

        if not isinstance(exc, (ApprovalAlreadyDecided, RunActive)):
            return None
        stored = await self._store.get_approval(run_id, approval_id)
        if stored.status != "pending" and _same_decision(
            stored, decision, decision_payload
        ):
            return stored
        return None

    async def _parse_edited_plan(
        self,
        run_id: str,
        plan_body: dict[str, Any],
        visible_to: "UserContext | None",
    ) -> tuple[PlanRecord, list[PlanTaskRecord]]:
        # Persisted plans can carry a planner-selected model tier. Editing
        # canonicalizes that field away; model routing belongs to the server.
        # Copy only the affected structure, and leave malformed or unknown
        # fields intact so ordinary schema validation still rejects them.
        raw_tasks = plan_body.get("tasks")
        if isinstance(raw_tasks, list):
            plan_body = {
                **plan_body,
                "tasks": [
                    {
                        **task,
                        "params": {
                            key: value for key, value in task["params"].items()
                            if key != "model_tier"
                        },
                    }
                    if isinstance(task, dict) and isinstance(task.get("params"), dict)
                    else task
                    for task in raw_tasks
                ],
            }
        try:
            plan = ExecutionPlanModel.model_validate(plan_body)
        except ValidationError as exc:
            raise AgentControlValidationError(
                [
                    f"{'.'.join(str(part) for part in error['loc'])}: "
                    f"{error['msg']}"
                    for error in exc.errors()
                ]
            ) from exc
        budgeted = [
            task.id
            for task in plan.tasks
            if task.budget.model_dump(exclude_none=True)
        ]
        if budgeted:
            raise AgentControlValidationError(
                [
                    ("Task budgets are managed by the server; remove budget from these "
                    "tasks: ")
                    + ", ".join(budgeted)
                    + "."
                ],
                error_type="task_budget_server_managed",
            )
        run_summary = await self._run_summary(
            run_id, visible_to=visible_to
        )
        overrides = run_summary.get("agent_overrides") or {}
        depth = (
            str(overrides.get("depth") or "normal")
            if isinstance(overrides, dict)
            else "normal"
        )
        tier = (
            str(overrides.get("agent_tier") or "")
            if isinstance(overrides, dict)
            else ""
        )
        research_policy = derive_web_research_policy(
            depth=depth,
            edited_plan=True,
            tier=tier or None,
        )
        # THE shared collection rule (resolver + allowed-ids check) the
        # planner runs — a user edit naming a collection ("EU-AI-Act")
        # is canonicalized to its id, an invisible/unknown reference is a
        # 400 now instead of a task failure after approval (same
        # visibility rule, earlier surface). A missing knowledge
        # collaborator (memory/dev) skips the check: the runtime
        # per-collection gate still guards retrieval.
        catalog = await self._collection_catalog(run_id, visible_to)
        errors = (
            resolve_plan_collections(plan, catalog)
            if catalog is not None
            else []
        )
        errors += validate_plan(
            plan,
            max_tasks=self._max_plan_tasks,
            allowed_collection_ids=(
                {entry.collection_id for entry in catalog}
                if catalog is not None
                else None
            ),
            # Editing the plan is itself the explicit research consent.
            web_research_allowed=research_policy.allowed,
            web_research_profile=(
                None
                if research_policy.max_profile is not None
                else research_policy.profile
            ),
            web_research_profile_ceiling=research_policy.max_profile,
            max_web_instant_tasks=research_policy.max_instant_tasks,
        )
        plan_id = f"plan_{uuid.uuid4().hex}"
        record = PlanRecord(
            plan_id=plan_id,
            run_id=run_id,
            version=0,
            status="approved",
            created_by="user",
            summary_markdown=plan.summary_markdown,
            assumptions=tuple(
                without_stale_web_note(
                    plan.assumptions,
                    (task.tool_kind for task in plan.tasks),
                )
            ),
            success_criteria=tuple(plan.success_criteria),
            reason="user_edit",
            created_at=time.time(),
        )
        tasks = [
            PlanTaskRecord(
                task_id=task.id,
                plan_id=plan_id,
                run_id=run_id,
                ordinal=index,
                title=task.title,
                tool_kind=task.tool_kind,
                objective=task.objective,
                queries=tuple(task.queries),
                gap_ids=tuple(task.gap_ids),
                depends_on=tuple(task.depends_on),
                budget={},
                params=task.params.model_dump(exclude_none=True),
                expected_output=task.expected_output,
            )
            for index, task in enumerate(plan.tasks)
        ]
        previous_tasks: list[PlanTaskRecord] = []
        try:
            _previous_plan, previous_tasks = await self._store.get_plan(run_id)
        except PlanNotFound:
            pass
        errors += additive_replan_errors(previous_tasks, tasks)
        if errors:
            raise AgentControlValidationError(errors)
        tasks = carry_forward_terminal_task_results(previous_tasks, tasks)
        return record, tasks

    async def _rules_in_force(
        self, run_id: str, approval: ApprovalRecord
    ) -> dict[str, tuple[dict[str, Any], str]]:
        """The rules the run works under when *approval* is decided, by id.

        The requirement in force is the one the last EARLIER plan or replan
        decision set, else the submitted one — the record the mission's
        own state follows from submission through every gate. Each rule
        comes with the composed block the run is frozen under, read back
        from that text (``(part, block)``). Decisions after *approval*
        never count. A decided approval never gets here: its replay is
        compared with what it stored (:func:`_stored_requirement`).
        """
        approvals = await self._store.list_approvals(run_id)  # newest first
        position = [record.approval_id for record in approvals].index(
            approval.approval_id
        )
        composed: str | None = None
        rules: list[Any] = []
        for record in approvals[position + 1:]:
            payload = record.decision_payload
            if (
                record.kind in ("plan", "replan")
                and record.decision in ("approve", "edit")
                and "report_guidance" in payload
            ):
                composed = str(payload.get("report_guidance") or "")
                rules = list(
                    decided_requirement_parts(
                        payload, approval_id=record.approval_id
                    ).get("rules")
                    or []
                )
                break
        if composed is None:
            body = await asyncio.to_thread(
                self._run_store.execution_request_body, run_id
            )
            composed = str(body.get("report_requirement") or "")
            rules = list(body.get("report_rules") or [])
        if not rules:
            return {}
        split = split_composed_requirement(
            composed, [str(rule.get("label") or "") for rule in rules]
        )
        if split is None:
            log.warning(
                "Run %s: the requirement in force at approval %s does not "
                "split into its %d rule(s); every rule this decision "
                "attaches is resolved from the catalog again.",
                run_id,
                approval.approval_id,
                len(rules),
            )
            return {}
        return {
            str(rule["template_id"]): (dict(rule), block)
            for rule, block in zip(rules, split[0], strict=True)
        }

    async def _collection_catalog(
        self,
        run_id: str,
        visible_to: "UserContext | None",
    ) -> list[CollectionCatalogEntry] | None:
        """Caller-visible, run-admitted catalog for edit canonicalization.

        ``None`` degrades (no knowledge collaborator wired — memory/dev);
        the runtime per-collection gate still guards retrieval. A scoped
        run's persisted collection IDs are authoritative, including an
        explicit empty list. The live listing is intersected with that
        immutable boundary, so a share accepted while a plan is parked
        cannot expand the run.
        """
        if self._knowledge is None:
            return None
        request_body = await asyncio.to_thread(
            self._run_store.execution_request_body, run_id
        )
        knowledge_filters = request_body.get("knowledge_filters")
        if knowledge_filters is None:
            knowledge_filters = {}
        if not isinstance(knowledge_filters, dict):
            raise RuntimeError(
                "Persisted run knowledge filters are invalid."
            )
        context = visible_to
        if context is None:
            context = await self._execution_context(run_id)
        admitted_ids = pinned_knowledge_collection_ids(
            knowledge_filters,
            scoped_principal=(
                context is not None
                and context.principal.user_id is not None
            ),
        )
        if admitted_ids == frozenset():
            return []
        collections = await self._knowledge.list_collections(
            visible_to=context
        )
        return [
            CollectionCatalogEntry(
                collection_id=str(item.id),
                name=str(item.name),
            )
            for item in collections
            if admitted_ids is None or str(item.id) in admitted_ids
        ]

    # -- clarifications ------------------------------------------------------ #

    async def list_clarifications(self, run_id: str) -> list[ClarificationRecord]:
        return await self._store.list_clarifications(run_id)

    async def answer_clarification(
        self,
        *,
        run_id: str,
        clarification_id: str,
        answer: str | None,
        option_id: str | None,
        answers: dict[str, Any] | None = None,
        principal: "Principal | None",
    ) -> tuple[ClarificationRecord, dict[str, Any], bool]:
        """Answer one clarification; returns (record, run summary, replayed).

        Exactly one of *answer* / *option_id* / *answers* must be given.
        An option id must exist in the clarification's legacy options;
        a structured *answers* map must resolve EVERY question of the
        round (option ids subset of that question's options, at most one
        pick unless ``multi_select``, and each question needs at least
        one pick or non-empty free text) — the round parks the run once,
        so a partial answer would strand the remaining questions.
        """
        visible_to = self._caller_context(principal)
        has_answer = bool(answer and answer.strip())
        has_option = bool(option_id)
        has_answers = bool(answers)
        if (has_answer + has_option + has_answers) != 1:
            raise AgentControlValidationError(
                ["Provide exactly one of answer, option_id or answers."]
            )
        clarification = await self._store.get_clarification(
            run_id, clarification_id
        )
        if has_option:
            known = {
                str(option.get("id", ""))
                for option in clarification.options
            }
            if option_id not in known:
                raise AgentControlValidationError(
                    [f'Unknown option {option_id!r}.']
                )
        normalized_answers: dict[str, Any] = {}
        if has_answers:
            normalized_answers = _validated_round_answers(
                clarification, answers or {}
            )
        answered_by = principal.user_id if principal is not None else None
        normalized_answer = (answer or "").strip()
        normalized_option = option_id or ""
        if clarification.status != "pending":
            if (
                clarification.answer == normalized_answer
                and clarification.option_id == normalized_option
                and clarification.answers == normalized_answers
            ):
                summary = await self._run_summary(
                    run_id, principal=principal, visible_to=visible_to
                )
                return clarification, summary, True
            raise ClarificationAlreadyAnswered(clarification)
        replayed = False
        try:
            clarification, summary = (
                await self._store.answer_clarification_and_resume(
                    run_id=run_id,
                    clarification_id=clarification_id,
                    answer=normalized_answer,
                    option_id=normalized_option,
                    answers=normalized_answers,
                    answered_by_user_id=answered_by,
                    resume=self._resume_callable(run_id, principal),
                )
            )
        except Exception as exc:
            # Same race window as approvals — see _approval_replay.
            replay = await self._clarification_replay(
                exc,
                run_id,
                clarification_id,
                normalized_answer,
                normalized_option,
                normalized_answers,
            )
            if replay is None:
                raise
            clarification = replay
            summary = await self._run_summary(
                run_id, principal=principal, visible_to=visible_to
            )
            replayed = True
        if not replayed:
            # The answer row and resume committed together before the signal.
            # Emitting earlier would leave a false answered event when live
            # authority or the waiting->queued CAS rejects the composed write.
            await self._emit(
                run_id,
                CLARIFICATION_ANSWERED_EVENT,
                {"clarification_id": clarification_id},
            )
            await self._record_audit(
                principal,
                action="agent.clarification_answered",
                run_id=run_id,
                detail={"clarification_id": clarification_id},
            )
        return clarification, summary, replayed

    async def _clarification_replay(
        self,
        exc: Exception,
        run_id: str,
        clarification_id: str,
        answer: str,
        option_id: str,
        answers: dict[str, Any],
    ) -> ClarificationRecord | None:
        from inqtrix.server.runs import RunActive

        if not isinstance(exc, (ClarificationAlreadyAnswered, RunActive)):
            return None
        stored = await self._store.get_clarification(run_id, clarification_id)
        if (
            stored.status != "pending"
            and stored.answer == answer
            and stored.option_id == option_id
            and stored.answers == answers
        ):
            return stored
        return None

    # -- artifacts ------------------------------------------------------------ #

    async def list_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[ArtifactRecord], str | None]:
        return await self._store.list_artifacts(
            run_id, kind=kind, limit=limit, after=after
        )

    async def get_artifact(
        self, run_id: str, artifact_id: str, *, revision: int | None = None
    ) -> tuple[ArtifactRecord, list[ArtifactRevisionRecord]]:
        return await self._store.get_artifact(
            run_id, artifact_id, revision=revision
        )

    async def list_session_artifacts(
        self, session_id: str
    ) -> list[ArtifactRecord]:
        """ALL session-scoped artifacts, oldest first, META-ONLY.

        Anchor-independent by design: a session artifact's ``run_id``
        moves to the newest updating run, so run-scoped listings lose it
        — this is the ONE listing that never does, because it keys on the
        session id instead of a run anchor.
        """
        return await self._store.list_session_artifacts(session_id)

    async def user_update_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        content_markdown: str,
        expected_revision: int,
        principal: "Principal | None",
        visible_to: "UserContext | None" = None,
        workspace_id: str | None = None,
    ) -> ArtifactRecord:
        """Commit a user edit, audit it and notify any still-live run stream."""
        artifact = await self._store.user_update_artifact(
            run_id=run_id,
            artifact_id=artifact_id,
            content_markdown=content_markdown,
            expected_revision=expected_revision,
            authorize=self._authorized_control_callable(
                run_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            ),
        )
        await self._emit_user_artifact_update(
            run_id,
            {
                "artifact_id": artifact.artifact_id,
                "revision": artifact.revision,
                "updated_by": "user",
            },
            principal=principal,
            visible_to=visible_to,
        )
        await self._record_audit(
            principal,
            action="agent.artifact_edited",
            run_id=run_id,
            detail={
                "artifact_id": artifact_id,
                "revision": str(artifact.revision),
            },
        )
        return artifact

    async def rename_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        title: str,
        principal: "Principal | None",
        visible_to: "UserContext | None" = None,
        workspace_id: str | None = None,
    ) -> ArtifactRecord:
        """Metadata-only user rename with a signal for a still-live run.

        The event stays deliberately kind-less like the user PUT (it
        must never chip) and carries the new title plus ``renamed`` so
        clients refresh names without treating it as a content write.
        """
        artifact = await self._store.rename_artifact(
            run_id=run_id,
            artifact_id=artifact_id,
            title=title,
            authorize=self._authorized_control_callable(
                run_id,
                principal=principal,
                visible_to=visible_to,
                workspace_id=workspace_id,
            ),
        )
        await self._emit_user_artifact_update(
            run_id,
            {
                "artifact_id": artifact.artifact_id,
                "revision": artifact.revision,
                "title": artifact.title,
                "updated_by": "user",
                "renamed": True,
            },
            principal=principal,
            visible_to=visible_to,
        )
        await self._record_audit(
            principal,
            action="agent.artifact_renamed",
            run_id=run_id,
            detail={
                "artifact_id": artifact_id,
                "revision": str(artifact.revision),
            },
        )
        return artifact

    async def export_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        title: str | None,
        folder_id: str | None,
        principal: "Principal | None",
        caller_user_id: uuid.UUID | None,
        workspace_id: str | None,
    ) -> dict[str, Any]:
        """Copy an artifact into a NEW editor document.

        Not idempotent by design — every export creates a fresh document
        and remains separate from the patch-application path.

        Raises:
            AgentControlUnavailable: Editor persistence is not wired (502).
            ArtifactNotFound: Unknown artifact.
        """
        if self._editor_persistence is None:
            raise AgentControlUnavailable(
                "Editor persistence is unavailable."
            )
        artifact, _revisions = await self._store.get_artifact(
            run_id, artifact_id
        )
        now = time.time()
        document = await self._editor_persistence.save_document(
            id=f"editor-doc-{uuid.uuid4().hex[:16]}",
            title=title or artifact.title or "Agent-Memo",
            content_markdown=artifact.content_markdown,
            folder_id=folder_id,
            # Distinct from a native research-report import: the UI reads
            # this + source_run_id to show agent-run provenance.
            source="agent-artifact",
            source_run_id=run_id,
            revision=1,
            diff_anchor_markdown=None,
            diff_anchor_updated_at=None,
            created_at=now,
            updated_at=now,
            caller_user_id=caller_user_id,
            workspace_id=workspace_id,
            visible_to=None,
        )
        await self._record_audit(
            principal,
            action="agent.artifact_exported",
            run_id=run_id,
            detail={"artifact_id": artifact_id, "document_id": document.id},
        )
        return {
            "id": document.id,
            "title": document.title,
            "folder_id": document.folder_id,
            "source": document.source,
            "source_run_id": document.source_run_id,
            "revision": document.revision,
            "created_at": document.created_at,
            "updated_at": document.updated_at,
        }

    # -- composition helpers ---------------------------------------------------- #

    def _authorized_control_callable(
        self,
        run_id: str,
        *,
        principal: "Principal | None",
        visible_to: "UserContext | None" = None,
        workspace_id: str | None = None,
    ) -> Callable[[Any], Awaitable[Any]]:
        """Build the live caller-authority callback for a control mutation."""
        caller = self._caller_context(principal, visible_to)

        async def _authorize(control_write: Any) -> Any:
            return await asyncio.to_thread(
                self._run_store.authorized_control_write,
                run_id,
                workspace_id=workspace_id,
                visible_to=caller,
                control_write=control_write,
            )

        return _authorize

    def _resume_callable(
        self, run_id: str, principal: "Principal | None"
    ):
        """The resume hook the store composes its decision write with.

        Awaitable so the blocking run-store call leaves the event loop;
        the durable store forwards *control_write* into the resume
        transaction, the memory store passes ``None``.
        """

        async def _resume(control_write: Any) -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "actor_user_id": (
                    principal.user_id if principal is not None else None
                ),
                "execution_scopes": (
                    principal.scopes if principal is not None else frozenset()
                ),
            }
            if control_write is not None:
                kwargs["control_write"] = control_write
            return await asyncio.to_thread(
                self._run_store.resume_run,
                run_id,
                **kwargs,
            )

        return _resume

    async def _emit_user_artifact_update(
        self,
        run_id: str,
        payload: dict[str, Any],
        *,
        principal: "Principal | None",
        visible_to: "UserContext | None",
    ) -> None:
        """Late user edits persist independently of the finished run log.

        The store retains its atomic terminal guard if a live run finishes
        between this scoped read and emit. Unexpected drops remain visible.
        """
        try:
            summary = await self._run_summary(
                run_id, principal=principal, visible_to=visible_to,
            )
        except Exception:  # noqa: BLE001 — notification cannot undo a committed edit
            log.warning(
                "Run status could not be read for the artifact notification.",
                exc_info=True,
            )
            return
        if summary.get("status") in TERMINAL_RUN_STATUS_VALUES:
            return
        await self._emit(run_id, ARTIFACT_UPDATED_EVENT, payload)

    async def _emit(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        """Append one signal event; a failure is loud but non-fatal.

        Signals never gate the row truth: approval decisions,
        clarification answers, and artifact edits emit only after their
        authoritative write commits. Off-loop via ``to_thread`` — the durable
        store blocks on a database round-trip.
        """
        try:
            await asyncio.to_thread(
                self._run_store.emit, run_id, event_type, payload
            )
        except Exception:  # noqa: BLE001 — signal only, never roll back rows
            log.warning(
                "Agent event %s for run %s could not be appended.",
                event_type,
                run_id,
                exc_info=True,
            )

    async def _record_audit(
        self,
        principal: "Principal | None",
        *,
        action: str,
        run_id: str,
        detail: dict[str, str],
    ) -> None:
        if self._audit is None or principal is None:
            return
        await self._audit.record(
            AuditEntry(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=action,
                resource_type="run",
                resource_id=run_id,
                detail=detail,
            )
        )
