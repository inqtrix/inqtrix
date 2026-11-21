"""Orchestration for the agent control surface (approvals, clarifications,
artifacts).

Sits between the thin routers in
:mod:`inqtrix.server.routers.agent_runs` and the store pair behind
:class:`~inqtrix.agents.control_ports.AgentControlStore`. Responsibilities:

* compose interrupt resolutions with the RUN STORE so the decision write
  and the ``waiting -> queued`` flip stay atomic per backend (the store's
  ``*_and_resume`` methods own the mechanics, this service supplies the
  resume callable);
* validate edit decisions of a tool approval against the input schema of
  the tool it gates;
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

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from inqtrix.agents.control_ports import (
    APPROVAL_DECISIONS,
    APPROVAL_STATUS_BY_DECISION,
    DECIDABLE_APPROVAL_KINDS,
    ApprovalAlreadyDecided,
    ApprovalRecord,
    ArtifactRecord,
    ArtifactRevisionRecord,
    ClarificationAlreadyAnswered,
    ClarificationRecord,
)
from inqtrix.auth.permissions import AuditEntry
from inqtrix.exceptions import RunNotFound
from inqtrix.runs.records import RunActive
from inqtrix.runs.shared import TERMINAL_RUN_STATUS_VALUES
from inqtrix.harness.contracts.events import (
    APPROVAL_DECIDED_EVENT,
    ARTIFACT_UPDATED_EVENT,
    CLARIFICATION_ANSWERED_EVENT,
)

if TYPE_CHECKING:
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.runs.ports import RunStorePort
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )


log = logging.getLogger(__name__)


class AgentControlValidationError(ValueError):
    """Invalid decision/answer/edit input (HTTP 400).

    Attributes:
        errors: Every collected violation, reported at once so the caller
            fixes the input in one round.
        reason: A machine-readable reason the response carries beside the
            type, or ``None``.
    """

    def __init__(
        self,
        errors: list[str],
        *,
        error_type: str = "invalid_request_error",
        reason: str | None = None,
    ) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
        self.error_type = error_type
        self.reason = reason


class AgentControlUnavailable(RuntimeError):
    """A required collaborator is not wired (HTTP 502, loud)."""


def _same_decision(
    stored: ApprovalRecord, decision: str, decision_payload: dict[str, Any]
) -> bool:
    """Replay identity: verb AND payload (the edited actions) must match."""
    return (
        stored.decision == decision
        and dict(stored.decision_payload) == decision_payload
    )


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
        durable: bool = False,
    ) -> None:
        self._store = store
        self._run_store = run_store
        self._audit = audit
        self._editor_persistence = editor_persistence
        self._durable = durable

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
        principal = await self._run_store.aexecution_principal(run_id)
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
        return await self._run_store.aget(run_id, visible_to=context)

    # -- terminal runs ------------------------------------------------------ #

    async def reconcile_terminal_control_rows(self, run_id: str) -> bool:
        """Settle the control rows a cancelled or failed agent run left open.

        Run terminal state and control rows live in separate ports. A run
        that ends normally releases its canvas itself; a run ended from
        outside may not reach that code. This idempotent repair releases
        artifacts left ``writing`` and closes approvals left ``pending``,
        without adding another event consumer or persistence layer. The
        cancel route calls it for every run of the cancelled tree
        (:meth:`reconcile_terminal_run_tree`); a run the system ends (binding
        end, wait sweep and expiry, restart sweep) does not pass through it.

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
            await self.reconcile_terminal_control_rows(run_id)

    # -- approvals --------------------------------------------------------- #

    async def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        return await self._store.list_approvals(run_id)

    async def decide_approval(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        note: str,
        principal: "Principal | None" = None,
        visible_to: "UserContext | None" = None,
        actions_body: list[Any] | None = None,
        approval_scope: str = "",
    ) -> tuple[ApprovalRecord, dict[str, Any], bool]:
        """Resolve one approval; returns (approval, run summary, replayed).

        ``replayed`` is True when the SAME decision was already recorded —
        the idempotent 200 path (no second resume, no second event).

        Only a ``kind="tool"`` approval is decided. A stored approval of
        another kind of :data:`~inqtrix.agents.control_ports.APPROVAL_KINDS`
        belongs to the retired mission workflow: it stays readable, and a
        decision on it is refused with the reason ``workflow_retired`` before
        a replay of the decision it stored would answer.

        ``actions_body`` carries the edit of a tool approval: the revised
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
        like a retry carrying different edited actions.
        ``ALWAYS_GATED_TOOLS`` are never grantable.

        Raises:
            AgentControlValidationError: Unknown decision verb, a stored
                approval of a retired kind (reason ``workflow_retired``),
                edit without/with an invalid actions body, actions without
                an edit, or an invalid ``approval_scope``.
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
        if approval.kind not in DECIDABLE_APPROVAL_KINDS:
            raise AgentControlValidationError(
                [
                    f"An approval of kind {approval.kind} belongs to the retired "
                    "mission workflow and can no longer be decided."
                ],
                reason="workflow_retired",
            )
        decision_payload: dict[str, Any] = {}
        if decision == "edit":
            decision_payload = {
                "actions": _validated_tool_edit(approval, actions_body)
            }
        elif actions_body is not None:
            raise AgentControlValidationError(
                ["An actions body is allowed only for decision=edit."]
            )
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
            # stored state, a different one conflicts. For edit the actions
            # payload IS the decision — a retry carrying DIFFERENT actions
            # must conflict, never be swallowed as a replay.
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
        if not isinstance(exc, (ApprovalAlreadyDecided, RunActive)):
            return None
        stored = await self._store.get_approval(run_id, approval_id)
        if stored.status != "pending" and _same_decision(
            stored, decision, decision_payload
        ):
            return stored
        return None


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
        from inqtrix.runs.records import RunActive

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
            return await self._run_store.aauthorized_control_write(
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
            return await self._run_store.aresume_run(
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
        authoritative write commits.
        """
        try:
            await self._run_store.aemit(run_id, event_type, payload)
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
