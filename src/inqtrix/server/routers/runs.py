"""Native run lifecycle endpoints (``/v1/runs*``).

Submission dispatches through the
:class:`~inqtrix.services.run_service.RunService`; the read side
(list/get/result/cancel/events) consumes the in-memory run store
directly — it already is the repository surface.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from queue import Empty
from typing import TYPE_CHECKING, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from inqtrix.auth.principal import (
    Principal,
    UserContext,
    resolve_live_principal,
)
from inqtrix.core.constants import (
    AGENT_MODE_IDS,
    AGENT_SOURCE_ACCESS,
    AGENT_SOURCE_IDS,
    AGENT_TOOL_DIRECTIVES,
)
from pydantic import ValidationError

from inqtrix.agents.report_requirement import REPORT_GUIDANCE_MAX_CHARS
from inqtrix.services.attached_report_resolver import (
    AttachedReportError,
    resolve_attached_reports,
)
from inqtrix.services.report_requirement_resolver import (
    ReportRequirementError,
    resolve_report_requirement,
)

from inqtrix.core.results import (
    CANVAS_COMMENT_MAX_CHARS,
    CANVAS_CONTEXT_MAX_COMMENTS,
    CANVAS_QUOTE_CONTEXT_MAX_CHARS,
    CANVAS_QUOTE_MAX_CHARS,
    CanvasContext,
    SourcePolicy,
)
from inqtrix.content.skills import SkillNotFound
from inqtrix.exceptions import RunCleanupFailed
from inqtrix.execution_authority import (
    KNOWLEDGE_SCOPE_ALL,
    knowledge_store_required,
    requested_knowledge_scope,
)
from inqtrix.knowledge.stores.ports import CollectionNotFound
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.project.knowledge_sessions_ports import KnowledgeSessionNotFound
from inqtrix.quota.models import QuotaDimension
from inqtrix.result import merge_knowledge_result_payload
from inqtrix.server.metrics import record_admission_rejected
from inqtrix.server.routers import (
    quota_admission,
    quota_record,
    stack_error_response,
)
from inqtrix.runs.shared import replay_after
from inqtrix.server.stream_authorization import (
    GenerationGatedFrameAuthorization,
)
from inqtrix.server.runs import (
    RunActive,
    RunNotFound,
    RunPerUserLimit,
    RunQueueFull,
    RunSessionActive,
    format_sse_event,
)
from inqtrix.pagination import (
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    list_envelope,
)
from inqtrix.services.agent_context import StackResolutionError
from inqtrix.services.system_runtime import knowledge_availability
from inqtrix.services.request_parsing import (
    error_response,
    format_history,
    incognito_from_request,
    question_and_messages,
    ui_language_from_request,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

TERMINAL_EVENTS = {
    "inqtrix.run.completed",
    "inqtrix.run.failed",
    "inqtrix.run.cancelled",
}


_SERVER_SET_RUN_FIELDS = frozenset({"work_profile", "handover"})
"""Request fields that turn a run into an internal role of another run. Only
server code sets them, on the child submission seam."""

_SERVER_RESOLVED_RUN_FIELDS = frozenset({"report_rules", "report_requirement"})
"""Request fields the server derives from what the client attached. The
rule parts are resolved from the caller's own catalog at submission and
the requirement text is composed from them and the typed guidance; a
client value would name rules or revisions the run never resolved, or
carry text no resolver composed."""


def _parse_agent_source_policy(
    body: Mapping[str, object],
) -> SourcePolicy:
    """Validate the additive Agent Desk source wire contract.

    Web is the session's switch; project knowledge follows the attached
    collections (the composer derives it). Durable replay receives the
    normalized value and validates it again through
    :class:`~inqtrix.core.results.RunRequest`.

    Raises:
        ValueError: With a user-facing validation message.
    """
    raw_policy = body.get("source_policy")
    if raw_policy is None:
        return SourcePolicy()
    if not isinstance(raw_policy, dict):
        raise ValueError("source_policy must be an object.")
    unknown = sorted(set(raw_policy) - set(AGENT_SOURCE_IDS))
    if unknown:
        raise ValueError(
            "source_policy allows only: " + ", ".join(AGENT_SOURCE_IDS)
        )
    invalid = [
        key
        for key, value in raw_policy.items()
        if value not in AGENT_SOURCE_ACCESS
    ]
    if invalid:
        raise ValueError(
            "source_policy values must be available or disabled."
        )
    return SourcePolicy.model_validate(raw_policy)


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the native run routes against the container."""
    router = APIRouter()
    settings = container.settings
    resolver = container.resolver
    lanes = container.execution_lanes
    run_service = container.run_service
    run_store = container.run_store
    quota_service = container.quota_service
    principal_dep = container.principal_dependency
    user_context_dep = container.user_context_dependency
    share_service = container.share_service
    workspace_admin = container.workspace_admin
    knowledge_service = container.knowledge_service
    skill_service = container.skill_service

    @router.post("/v1/runs")
    async def create_run(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Create a queued native research run for browser UI clients."""
        try:
            body = await req.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        reserved = sorted(
            (_SERVER_SET_RUN_FIELDS | _SERVER_RESOLVED_RUN_FIELDS) & set(body)
        )
        if reserved:
            # Refused, not ignored: a client that believes it started a
            # role run, or attached rules by naming their parts or their
            # composed text, must learn that it did not.
            return error_response(
                400,
                f"{', '.join(reserved)} can only be set by the server.",
                "invalid_request_error",
            )

        try:
            source_policy = _parse_agent_source_policy(body)
        except ValueError as exc:
            return error_response(
                400, str(exc), "invalid_request_error"
            )

        resolution_body = dict(body)

        try:
            workspace_id = workspace_id_from_request(req, body)
            question, messages = question_and_messages(body, settings.server)
            incognito = incognito_from_request(body)
            resolved = resolver.resolve(resolution_body)
            ui_language = ui_language_from_request(body, resolved.mode)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except StackResolutionError as exc:
            return stack_error_response(exc)

        is_agent = resolved.mode in AGENT_MODE_IDS

        # Pin every knowledge-capable run to a concrete collection set at
        # submission. Explicit ids are strict (one invisible id denies the
        # whole request); an omitted set becomes every collection visible to
        # this actor. The ids ride the durable request/checkpoint and are
        # re-authorized at execution safepoints. A resumed segment can
        # therefore fail closed on lost access but can never silently gain a
        # newly visible source or continue with a reduced corpus.
        try:
            requested_scope, requested_collections = requested_knowledge_scope(
                resolved.knowledge_filters
            )
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        # The session switch ("/wissen", scope "all") is the user's explicit
        # choice exactly like a chip: it resolves to every collection the
        # caller may see and is pinned under that identity. An unscoped
        # principal (auth mode none/apikey) has no visibility boundary to
        # resolve; its runs keep the tenant-wide view they always had —
        # recorded as the explicit switch, never widened beyond it.
        explicit_scope = requested_scope is not None
        # The persisted scope label names the boundary that actually
        # executes: the switch only when no chip narrowed it. A request
        # carrying both keys is pinned to its chips and must not report
        # "all" afterwards.
        if requested_scope == KNOWLEDGE_SCOPE_ALL:
            resolved.knowledge_filters["scope"] = KNOWLEDGE_SCOPE_ALL
        else:
            resolved.knowledge_filters.pop("scope", None)
        # Persist WHOSE choice the pinned scope is. The pin below
        # resolves both cases to a concrete id list, so afterwards the
        # list alone can no longer tell "user picked these collections"
        # from "we filled in everything visible" — and the balanced gate
        # asks about exactly that difference. Written unconditionally so
        # a missing key means a row persisted before this flag existed
        # and fails closed to False.
        resolved.knowledge_filters["explicit"] = explicit_scope
        if knowledge_store_required(
            mode=resolved.mode, explicit_scope=explicit_scope
        ):
            # The same derivation the capability manifest publishes decides
            # before any cost: an explicit knowledge request while the store
            # is not configured or does not answer its bounded probe is
            # refused with the reason, not started and failed at the first
            # tool call. Only the vector-store probe runs here.
            knowledge_available, knowledge_reason = await knowledge_availability(
                container
            )
            if not knowledge_available:
                record_admission_rejected("knowledge_store_unavailable")
                return error_response(
                    503,
                    "The knowledge store is not available; the run was not "
                    "started. Retry when the store is back or remove the "
                    "knowledge scope.",
                    "knowledge_store_unavailable",
                    reason=knowledge_reason,
                )
        if (
            knowledge_service is not None
            and visible_to is not None
            and (explicit_scope or resolved.mode == "knowledge" or is_agent)
        ):
            try:
                resolved.knowledge_filters["collection_ids"] = (
                    await knowledge_service.resolve_ask_scope(
                        requested_collections or None,
                        visible_to=visible_to,
                    )
                )
            except CollectionNotFound:
                return error_response(
                    404, "Collection not found", "not_found"
                )

        if len(question) > resolved.agent_settings.max_question_length:
            return error_response(
                400,
                (
                    f'Question too long ({len(question)} characters, max. {resolved.agent_settings.max_question_length})'
                ),
                "invalid_request_error",
            )

        history = format_history(
            messages, max_messages=settings.server.max_messages_history
        )

        # Workspace-agent submission extras: validated here,
        # inert for every other mode. The agent kind drives the additive
        # summary keys and the cancel cascade.
        autonomy = body.get("autonomy")
        if autonomy is not None and autonomy not in (
            "strict",
            "balanced",
            "autonomous",
        ):
            return error_response(
                400,
                "autonomy must be strict, balanced or autonomous.",
                "invalid_request_error",
            )
        session_id = body.get("session_id")
        if session_id is not None and (
            not isinstance(session_id, str) or not session_id.strip()
        ):
            return error_response(
                400,
                "session_id must be a non-empty string.",
                "invalid_request_error",
            )
        document_id = body.get("document_id")
        if document_id is not None and (
            not isinstance(document_id, str) or not document_id.strip()
        ):
            return error_response(
                400,
                "document_id must be a non-empty string.",
                "invalid_request_error",
            )
        response_form = body.get("response_form")
        if response_form is not None and response_form not in (
            "auto",
            "chat",
            "canvas",
        ):
            return error_response(
                400,
                "response_form must be auto, chat or canvas.",
                "invalid_request_error",
            )
        if incognito and resolved.mode != "knowledge":
            return error_response(
                400,
                "incognito applies only to knowledge mode.",
                "invalid_request_error",
            )
        if incognito and session_id is not None:
            # A saved session keeps its answers; an incognito ask keeps none.
            return error_response(
                400,
                "An incognito ask cannot belong to a saved session.",
                "invalid_request_error",
            )
        is_saved_session_run = is_agent or resolved.mode == "knowledge"
        if session_id is not None and not is_saved_session_run:
            return error_response(
                400,
                "session_id applies only to agent or knowledge mode.",
                "invalid_request_error",
            )
        if response_form is not None and not is_agent:
            return error_response(
                400,
                "response_form applies only to agent modes.",
                "invalid_request_error",
            )
        if "source_policy" in body and not is_agent:
            return error_response(
                400,
                "source_policy applies only to agent modes.",
                "invalid_request_error",
            )
        # Canvas attachment: validated strictly, and only where it
        # is actually consumed — the kernel user message. Any other mode
        # REJECTS instead of silently dropping the attachment.
        raw_canvas_context = body.get("canvas_context")
        canvas_context = None
        if raw_canvas_context is not None:
            if resolved.mode != "agent_kernel":
                return error_response(
                    400,
                    "canvas_context applies only to agent kernel mode.",
                    "invalid_request_error",
                )
            try:
                canvas_context = CanvasContext.model_validate(
                    raw_canvas_context
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                location = ".".join(str(part) for part in first["loc"])
                return error_response(
                    400,
                    "Invalid canvas_context"
                    + (f" ({location}: {first['msg']})" if location else "")
                    + f' — Limits: max. {CANVAS_CONTEXT_MAX_COMMENTS} comments; quote {CANVAS_QUOTE_MAX_CHARS}, context {CANVAS_QUOTE_CONTEXT_MAX_CHARS}, comment {CANVAS_COMMENT_MAX_CHARS} characters. Content is never truncated during transfer.',
                    "invalid_request_error",
                )
        # Result requirement set BEFORE the run. The plan gate is
        # the other entry point, but three mission paths never reach one
        # — autonomous, the speed tier and delegated children — and the
        # kernel has no plan gate at all. Ids only: the server resolves
        # label, revision and body from the CALLER's own catalog, so a
        # client can never put unchecked text into the writing prompts.
        raw_report_guidance = body.get("report_guidance")
        raw_report_rule_ids = body.get("report_rule_ids")
        report_requirement = ""
        report_rules: list[dict[str, object]] = []
        if raw_report_guidance is not None or raw_report_rule_ids is not None:
            if not is_agent:
                return error_response(
                    400,
                    "report_guidance/report_rule_ids apply only to agent modes.",
                    "invalid_request_error",
                )
            if raw_report_guidance is not None and (
                not isinstance(raw_report_guidance, str)
                or len(raw_report_guidance) > REPORT_GUIDANCE_MAX_CHARS
            ):
                return error_response(
                    400,
                    f'report_guidance must be a string of at most {REPORT_GUIDANCE_MAX_CHARS} characters.',
                    "invalid_request_error",
                )
            # The shape only: the rule cap counts DISTINCT rules and is
            # enforced by the resolver both entry points share.
            if raw_report_rule_ids is not None and (
                not isinstance(raw_report_rule_ids, list)
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in raw_report_rule_ids
                )
            ):
                return error_response(
                    400,
                    "report_rule_ids must be a list of rule IDs.",
                    "invalid_request_error",
                )
            try:
                # The composed text is what the writers read; the parts
                # ({template_id, label, title, revision}) are what the user chose.
                # Both ride the durable request, the parts server-set only:
                # they bind the run's transcript to the revisions it is
                # frozen under.
                report_requirement, report_rules = await resolve_report_requirement(
                    free_text=(raw_report_guidance or "").strip(),
                    template_ids=raw_report_rule_ids or [],
                    prompt_templates=container.prompt_template_service,
                    visible_to=visible_to,
                )
            except ReportRequirementError as exc:
                # Loud, never silent: a run that quietly starts without
                # the requirements the user attached is the exact failure
                # this feature exists to prevent.
                return error_response(
                    400,
                    " ".join(exc.messages),
                    "invalid_request_error",
                )
        # Attached research reports: ids only. Existence, completion
        # and the name are read server-side under the CALLER's visibility,
        # like the prompt-library rules — a run whose attachments silently
        # vanished is the failure this channel exists to prevent.
        raw_report_ids = body.get("report_ids")
        attached_reports: list[dict[str, object]] = []
        if raw_report_ids is not None:
            if resolved.mode != "agent_kernel":
                return error_response(
                    400,
                    "report_ids applies only to agent kernel mode.",
                    "invalid_request_error",
                )
            if not isinstance(raw_report_ids, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_report_ids
            ):
                return error_response(
                    400,
                    "report_ids must be a list of run IDs.",
                    "invalid_request_error",
                )
            try:
                attached_reports = resolve_attached_reports(
                    raw_report_ids,
                    run_store=run_store,
                    visible_to=visible_to,
                )
            except AttachedReportError as exc:
                return error_response(
                    400, " ".join(exc.messages), "invalid_request_error"
                )
        if is_agent and autonomy is None:
            autonomy = settings.agent_platform.default_autonomy

        # Skill admission is strict like collections: ONE invisible
        # skill denies the whole submission (a run that
        # silently ran with fewer skills than attached would change
        # behavior without a trace), and the count cap is enforced
        # here, never prompted.
        raw_skill_ids = body.get("skill_ids")
        skill_ids: list[str] = []
        skill_revisions: dict[str, int] = {}
        if raw_skill_ids is not None:
            if not is_agent:
                return error_response(
                    400,
                    "skill_ids applies only to agent modes.",
                    "invalid_request_error",
                )
            if not isinstance(raw_skill_ids, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in raw_skill_ids
            ):
                return error_response(
                    400,
                    "skill_ids must be a list of non-empty strings.",
                    "invalid_request_error",
                )
            skill_ids = list(
                dict.fromkeys(item.strip() for item in raw_skill_ids)
            )
            max_attached = settings.agent_platform.skills_max_attached
            if len(skill_ids) > max_attached:
                return error_response(
                    400,
                    f'At most {max_attached} skills per run.',
                    "invalid_request_error",
                )
            if skill_ids and skill_service is None:
                return error_response(
                    400,
                    "Skills are not configured on this server.",
                    "invalid_request_error",
                )
            for skill_id in skill_ids:
                try:
                    record, _shared = await skill_service.get_visible(
                        skill_id,
                        tenant_id=principal.tenant_id,
                        visible_to=visible_to,
                    )
                    skill_revisions[skill_id] = record.revision
                except SkillNotFound:
                    return error_response(
                        404,
                        f'Skill not found: {skill_id}',
                        "not_found",
                    )
        raw_directives = body.get("tool_directives")
        tool_directives: list[str] = []
        if raw_directives is not None:
            if not is_agent:
                return error_response(
                    400,
                    "tool_directives applies only to agent modes.",
                    "invalid_request_error",
                )
            if not isinstance(raw_directives, list) or any(
                item not in AGENT_TOOL_DIRECTIVES for item in raw_directives
            ):
                return error_response(
                    400,
                    "tool_directives allows only: "
                    + ", ".join(AGENT_TOOL_DIRECTIVES),
                    "invalid_request_error",
                )
            tool_directives = list(dict.fromkeys(raw_directives))

        if is_agent and session_id is not None:
            try:
                await container.agent_sessions_service.claim_session(
                    session_id,
                    title=question[:120],
                    caller_user_id=(
                        principal.user_id
                        if principal.kind in ("oidc_session", "pat")
                        else None
                    ),
                    workspace_id=workspace_id,
                    visible_to=visible_to,
                )
            except AgentSessionNotFound:
                return error_response(
                    404, "Session not found", "not_found"
                )
        elif resolved.mode == "knowledge" and session_id is not None:
            try:
                await container.knowledge_sessions_service.claim_session(
                    session_id,
                    title=question[:120],
                    caller_user_id=(
                        principal.user_id
                        if principal.kind in ("oidc_session", "pat")
                        else None
                    ),
                    workspace_id=workspace_id,
                    visible_to=visible_to,
                )
            except KnowledgeSessionNotFound:
                return error_response(
                    404, "Session not found", "not_found"
                )

        # Quota admission: one run counts as one run-unit, AND the run is
        # the highest LLM-token consumer — so a caller already over their
        # monthly token budget is blocked here too (the run's token spend
        # is recorded post-hoc at completion; admission is the only gate
        # that works regardless of which process executes the run).
        # Blocked callers never enter the queue (429 before any cost).
        for dimension in (QuotaDimension.RUNS, QuotaDimension.LLM_TOKENS):
            denied = await quota_admission(quota_service, principal, dimension)
            if denied is not None:
                return denied

        try:
            # to_thread: the durable store's submit blocks on database
            # round-trips; running it inline would stall the event loop
            # for every concurrent request.
            summary = await asyncio.to_thread(
                run_service.submit,
                question=question,
                history=history,
                messages=messages,
                resolved=resolved,
                workspace_id=workspace_id,
                principal=principal,
                kind="agent" if is_agent else "standard",
                session_id=session_id,
                autonomy=(autonomy or "") if is_agent else "",
                document_id=(document_id or "") if is_agent else "",
                # "auto" is the wire default — stored as "" (no override,
                # the intake profile decides the deliverable form).
                response_form=(
                    ""
                    if not is_agent or response_form in (None, "auto")
                    else str(response_form)
                ),
                skill_ids=skill_ids,
                skill_revisions=skill_revisions,
                tool_directives=tool_directives,
                source_policy=source_policy if is_agent else None,
                canvas_context=canvas_context,
                report_requirement=report_requirement,
                report_rules=report_rules,
                attached_reports=attached_reports,
                incognito=incognito,
                ui_language=ui_language,
            )
        except RunPerUserLimit:
            # THEIR cap, not the shared queue: the caller can free
            # capacity by finishing/cancelling their own runs.
            record_admission_rejected("per_user_limit")
            return error_response(
                429,
                ("Your personal concurrent research limit has been reached. Please wait "
                "until your own jobs finish."),
                "rate_limit_error",
                reason="per_user_limit",
            )
        except RunQueueFull:
            record_admission_rejected("queue_full")
            return error_response(
                429,
                "Too many queued research jobs. Please wait.",
                "rate_limit_error",
                reason="queue_full",
            )
        except RunSessionActive:
            return error_response(
                409,
                "An agent run is already active in this session.",
                "session_run_active",
            )
        # Booked only once the run is accepted into the queue: the
        # run's LLM-token spend is booked separately at completion.
        await quota_record(quota_service, principal, QuotaDimension.RUNS, 1)
        return JSONResponse(status_code=202, content=summary)

    @router.post("/v1/runs/import")
    async def import_run(
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Persist a COMPLETED report carried in from a loaded project file.

        A report in a project file is a terminal-run snapshot with no
        server-side execution; importing stores it durably under the caller so
        it survives reload + follows the user across devices (the runs analogue
        of the chat/editor/prompt import-up). ``source_run_id`` is idempotent
        only inside the caller's scope; the server always allocates the public
        ``run_id``. The import never executes the agent (no quota cost).
        """
        try:
            body = await req.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            workspace_id = workspace_id_from_request(req, body)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)

        source_run_id = body.get("source_run_id")
        if not isinstance(source_run_id, str) or not source_run_id.strip():
            return error_response(
                400, "source_run_id is required.", "invalid_request_error"
            )

        def _epoch(value: object) -> float | None:
            return float(value) if isinstance(value, (int, float)) else None

        try:
            imported_snapshot = merge_knowledge_result_payload(
                body.get("snapshot")
                if isinstance(body.get("snapshot"), dict)
                else {}
            )
            imported_result = merge_knowledge_result_payload(
                body.get("result")
                if isinstance(body.get("result"), dict)
                else {},
                imported_snapshot,
            )
            summary = await asyncio.to_thread(
                run_store.import_completed_run,
                source_run_id=source_run_id,
                question=str(body.get("question") or ""),
                stack_name=str(body.get("stack") or "default"),
                result=imported_result,
                status=str(body.get("status") or "completed"),
                mode=str(body.get("mode") or "research"),
                agent_overrides=body.get("agent_overrides")
                if isinstance(body.get("agent_overrides"), dict)
                else {},
                snapshot=imported_snapshot,
                error=body.get("error")
                if isinstance(body.get("error"), dict)
                else None,
                created_at=_epoch(body.get("created_at")),
                workspace_id=workspace_id,
                created_by_user_id=principal.user_id,
                created_by_tenant_id=principal.tenant_id,
            )
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return JSONResponse(status_code=200, content=summary)

    @router.get("/v1/runs")
    def list_runs(
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List native runs, newest first, keyset-paginated.

        ``?limit`` (default 50, max 200) and ``?cursor`` page a long run
        history instead of materialising it whole on every poll. The
        envelope gains an additive ``next_cursor`` (``null`` on the last
        page); a client that ignores it and reads only ``data`` sees the
        newest page — the full working set for typical active-run counts.
        """
        try:
            workspace_id = workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        try:
            after = decode_cursor(req.query_params.get("cursor"))
        except InvalidCursor:
            return error_response(400, "Invalid cursor", "invalid_cursor")
        limit = clamp_limit(req.query_params.get("limit"))
        summaries, next_cursor = run_store.list_page(
            limit=limit,
            after=after,
            workspace_id=workspace_id,
            visible_to=visible_to,
        )
        return list_envelope(summaries, next_cursor)

    @router.get("/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Return the current public summary for one native run."""
        try:
            workspace_id = workspace_id_from_request(req)
            return run_store.get(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")

    @router.get("/v1/runs/{run_id}/result")
    def get_run_result(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Return the final report payload for a completed native run."""
        try:
            workspace_id = workspace_id_from_request(req)
            summary = run_store.get(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")
        if summary["status"] != "completed":
            return error_response(
                409,
                "Run has not completed yet",
                "run_not_completed",
                status=summary["status"],
            )
        try:
            stored_result = run_store.result(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            from inqtrix.evidence import project_web_search_payload

            return merge_knowledge_result_payload(
                project_web_search_payload(stored_result),
                summary.get("snapshot")
                if isinstance(summary.get("snapshot"), dict)
                else None,
            )
        except RunNotFound:
            return error_response(404, "Run result not found", "not_found")

    @router.post("/v1/runs/{run_id}/cancel")
    async def cancel_run(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Request cancellation for a queued or running native run."""
        try:
            workspace_id = workspace_id_from_request(req)
            summary, affected_run_ids = await asyncio.to_thread(
                run_store.cancel_tree,
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            # Waiting/queued runs become terminal synchronously and will not
            # re-enter the algorithm to close their plan rows. Running runs
            # retain their worker; its cancellation boundary performs the
            # same domain transition without racing this request.
            control_service = container.agent_control_service
            if control_service is not None:
                await control_service.reconcile_terminal_run_tree(
                    affected_run_ids
                )
            return summary
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")

    @router.delete("/v1/runs/{run_id}", status_code=204)
    async def delete_run(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
    ):
        """Permanently delete one terminal run (owner-only).

        Durable deletion is the only thing a reload respects: a run removed
        client-side alone re-hydrates from the store on the next list. The
        gate is creator identity (stronger than cancel, which a shared-in
        editor may call); only terminal runs delete (an active run is
        cancelled first). Deleting also revokes any shares so a recipient's
        regular resource list stops naming a run that no longer exists.
        """
        try:
            workspace_id = workspace_id_from_request(req)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        try:
            await asyncio.to_thread(
                run_store.delete,
                run_id,
                workspace_id=workspace_id,
                requester_user_id=principal.user_id,
            )
        except RunCleanupFailed:
            return error_response(503, "Run cleanup failed; the run is retained for retry.", "run_cleanup_failed")
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")
        except RunActive:
            return error_response(
                409,
                "Run is still active; cancel it first.",
                "run_active",
            )
        if (
            workspace_admin is not None
            and share_service is not None
            and not getattr(run_store, "atomic_resource_effects", False)
        ):
            revoked = await workspace_admin.revoke_shares_for_resource(
                tenant_id=principal.tenant_id,
                resource_type="run",
                resource_id=run_id,
                revoked_by_user_id=principal.user_id,
            )
            if revoked:
                log.info(
                    "Run %s deleted; %d shares revoked", run_id, revoked
                )

    @router.get("/v1/runs/{run_id}/children")
    async def run_children(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """List an agent run's direct child runs, newest first.

        Access is decided on the PARENT (view-share suffices for
        reading); children inherit that visibility through this route
        only — their direct URLs stay owner-scoped.
        """
        try:
            workspace_id = workspace_id_from_request(req)
            # to_thread like every durable-store touch in async routes:
            # the store call blocks on database round-trips.
            await asyncio.to_thread(
                run_store.get,
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")
        children = await asyncio.to_thread(run_store.children, run_id)
        return {"object": "list", "data": children}

    @router.get("/v1/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        req: Request,
        principal: Principal = Depends(principal_dep),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        """Stream buffered and live native run events as SSE.

        ``?after=<sequence>`` filters the REPLAY to events newer than
        the given sequence (reconnect semantics); the live
        tail is unaffected.
        """
        after_raw = req.query_params.get("after")
        after: int | None = None
        if after_raw is not None:
            try:
                after = int(after_raw)
            except ValueError:
                return error_response(
                    400,
                    "after must be an event sequence number (integer)",
                    "invalid_request_error",
                )
        # Decided BEFORE the subscribe: the JSON polling fallback is a
        # one-shot replay read — subscribing it as a live viewer would
        # spawn a throwaway poller thread per poll and count every ~3s
        # poll as a join in the inqtrix_stream_concurrent_viewers histogram.
        wants_json = req.query_params.get("format") == "json"
        try:
            workspace_id = workspace_id_from_request(req)
            subscription = await asyncio.to_thread(
                run_store.subscribe,
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
                stream=not wants_json,
            )
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except RunNotFound:
            return error_response(404, "Run not found", "not_found")

        async def _full_frame_check() -> bool:
            try:
                current = await resolve_live_principal(principal_dep, req)
                if (
                    current.user_id != principal.user_id
                    or current.kind != principal.kind
                    or current.tenant_id != principal.tenant_id
                    or current.session_id != principal.session_id
                    or current.pat_id != principal.pat_id
                    or current.scopes != principal.scopes
                ):
                    return False
                live_visible_to = (
                    await container.permission_service.resolve_user_context(
                        current
                    )
                )
                await asyncio.to_thread(
                    run_store.get,
                    run_id,
                    workspace_id=workspace_id,
                    visible_to=live_visible_to,
                )
            except (HTTPException, RunNotFound):
                return False
            return True

        # One shared gate with the indexing twin: the full chain above
        # re-runs only when the user's commit-ordered authorization
        # generation moved or the bounded ceiling elapsed. Principals
        # without a generation keep the full chain per frame.
        _frame_gate = GenerationGatedFrameAuthorization(
            full_check=_full_frame_check,
            read_generation=(
                (
                    lambda: container.permission_service
                    .authorization_generation(
                        tenant_id=principal.tenant_id,
                        user_id=principal.user_id,
                    )
                )
                if principal.user_id is not None
                else None
            ),
        )

        async def _authorized_frame() -> bool:
            return await _frame_gate.allowed()

        # The polling fallback returns the SAME replay buffer through
        # ``?format=json`` as an immediate JSON page instead of a
        # stream — for clients behind SSE-buffering proxies. One event
        # pipeline, one auth path; ``terminal`` tells the poller to stop.
        if wants_json:
            try:
                # One-shot request: a fresh gate would run the full chain
                # anyway, so the generation read would be pure added cost.
                if not await _full_frame_check():
                    return error_response(
                        404, "Run not found", "not_found"
                    )
                events = list(replay_after(subscription.replay, after))
                terminal = bool(
                    subscription.replay
                    and subscription.replay[-1].get("type")
                    in TERMINAL_EVENTS
                )
            finally:
                subscription.close()
            return {"object": "list", "data": events, "terminal": terminal}

        async def _event_generator():
            try:
                for event in replay_after(subscription.replay, after):
                    if not await _authorized_frame():
                        return
                    yield format_sse_event(event)
                # Terminal detection must use the UNFILTERED replay: a
                # reconnect with ``after`` at/past the terminal event
                # would otherwise wait forever on a stream that emits
                # nothing more.
                if subscription.replay and (
                    subscription.replay[-1].get("type") in TERMINAL_EVENTS
                ):
                    return
                # Replay/live boundary marker. Everything before this frame
                # was served from the buffer — HISTORY the client renders in
                # place; everything after arrives live and may animate. Pure
                # transport state: not persisted, carries no ``sequence`` (so
                # resume cursors ignore it), and terminal streams that end
                # right after the replay never emit it. Without the marker a
                # reload into a running run replayed its whole event history
                # one frame at a time and the UI animated every line as if it
                # were new.
                yield format_sse_event(
                    {"type": "inqtrix.stream.live", "run_id": run_id}
                )
                loop = asyncio.get_running_loop()
                next_heartbeat = loop.time() + 5.0
                while True:
                    if await req.is_disconnected():
                        return
                    try:
                        # Reader lane, not the shared default pool: a
                        # busy AI call must never delay event delivery.
                        event = await loop.run_in_executor(
                            lanes.streams,
                            partial(subscription.queue.get, True, 0.5),
                        )
                    except Empty:
                        if loop.time() >= next_heartbeat:
                            if not await _authorized_frame():
                                return
                            # SSE comments keep proxy/client readers active
                            # without creating a second event vocabulary or
                            # consuming sequence numbers.
                            yield ": keepalive\n\n"
                            next_heartbeat = loop.time() + 5.0
                        continue
                    if not await _authorized_frame():
                        return
                    yield format_sse_event(event)
                    next_heartbeat = loop.time() + 5.0
                    if event.get("type") in TERMINAL_EVENTS:
                        return
            finally:
                subscription.close()

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
