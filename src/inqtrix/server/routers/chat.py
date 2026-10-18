"""OpenAI-compatible ``/v1/chat/completions`` endpoint.

Streamed and non-streamed responses both dispatch the resolved mode through
the :class:`~inqtrix.core.algorithms.AlgorithmRegistry`: streaming via
:func:`guarded_stream` (which drives ``algorithm.run`` with a per-request
:class:`~inqtrix.core.context.RunContext`), the non-streaming path via the
:class:`~inqtrix.services.chat_service.ChatService`. Both share the single
graph seam ``inqtrix.research.web_research.run_web_graph``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.core.results import RunRequest
from inqtrix.knowledge.stores.ports import CollectionNotFound
from inqtrix.quota.models import QuotaDimension, consumed_tokens
from inqtrix.server.routers import (
    quota_admission,
    quota_record,
    stack_error_response,
)
from inqtrix.server.streaming import disconnect_watch, guarded_stream
from inqtrix.services.agent_context import StackResolutionError
from inqtrix.services.request_parsing import (
    format_history,
    incognito_from_request,
    question_and_messages,
    ui_language_from_request,
    workspace_id_from_request,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the chat-completions route against the container."""
    router = APIRouter()
    settings = container.settings
    resolver = container.resolver
    lanes = container.execution_lanes
    chat_service = container.chat_service
    knowledge_service = container.knowledge_service
    quota_service = container.quota_service
    user_context_dep = container.user_context_dependency
    async def _pin_ask_scope(
        resolved_filters: dict,
        mode: str,
        visible_to: "UserContext | None",
    ) -> JSONResponse | None:
        """Admission gate for asks against knowledge collections.

        Mirrors the native-runs gate. An explicit collection set is
        asserted strictly for EVERY mode — ONE invisible id denies the
        whole request, because silently answering from fewer collections
        than the caller picked would change the answer's meaning without
        a trace. An omitted/empty/null filter is PINNED to the
        caller-visible collections, but ONLY for ``mode=knowledge``: that
        algorithm consumes the filter without scoping of its own, while
        every other mode either ignores the filter or (workspace agent)
        resolves its scope itself at execution time. The pinned ids are
        written back into ``resolved_filters`` in place and ride into
        execution. ``visible_to=None`` (auth off) keeps the historical
        see-everything view untouched.
        """
        if knowledge_service is None or visible_to is None:
            return None
        requested = resolved_filters.get("collection_ids")
        explicit_scope = isinstance(requested, list) and bool(requested)
        if not explicit_scope and mode != "knowledge":
            return None
        try:
            resolved_filters["collection_ids"] = (
                await knowledge_service.resolve_ask_scope(
                    requested,
                    visible_to=visible_to,
                )
            )
        except CollectionNotFound:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "message": "Collection not found",
                    "type": "not_found",
                }},
            )
        return None

    @router.post("/v1/chat/completions")
    async def chat_completions(
        req: Request,
        principal: Principal = Depends(container.principal_dependency),
        visible_to: UserContext | None = Depends(user_context_dep),
    ):
        try:
            body = await req.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": {
                    "message": "Invalid JSON body",
                    "type": "invalid_request_error",
                }},
            )

        try:
            # Called for the 400 it raises on a malformed namespace. Chat has
            # no run listing to filter, so the value itself has no consumer.
            workspace_id_from_request(req, body)
            question, messages = question_and_messages(body, settings.server)
            incognito = incognito_from_request(body)
            resolved = resolver.resolve(body)
            ui_language = ui_language_from_request(body, resolved.mode)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except StackResolutionError as exc:
            return stack_error_response(exc)

        denied = await _pin_ask_scope(
            resolved.knowledge_filters,
            resolved.mode,
            visible_to,
        )
        if denied is not None:
            return denied

        if (
            not resolved.agent_settings.skip_search
            and len(question) > resolved.agent_settings.max_question_length
        ):
            return JSONResponse(
                status_code=400,
                content={"error": {
                    "message": (
                        f'Question too long ({len(question)} characters, max. {resolved.agent_settings.max_question_length})'
                    ),
                    "type": "invalid_request_error",
                }},
            )
        chat_agent_settings = resolver.chat_settings_for_question(
            resolved.agent_settings,
            question,
        )

        # Quota admission covers BOTH the streamed and non-streamed
        # branch (it runs before either): a caller over their monthly
        # LLM-token budget is blocked here, the real spend is recorded
        # after the answer is produced.
        denied = await quota_admission(
            quota_service, principal, QuotaDimension.LLM_TOKENS
        )
        if denied is not None:
            return denied

        history = format_history(
            messages, max_messages=settings.server.max_messages_history
        )

        stream = body.get("stream", False)
        include_progress_raw = body.get("include_progress", True)
        include_progress = (
            include_progress_raw
            if isinstance(include_progress_raw, bool)
            else True
        )

        sem = container.semaphore_factory()
        if sem.locked():
            return JSONResponse(
                status_code=429,
                content={"error": {
                    "message": "Too many concurrent requests. Please wait.",
                    "type": "rate_limit_error",
                }},
            )

        if stream:
            algorithm = container.registry.get(resolved.mode)
            if not algorithm.capabilities().get("supports_chat_completions"):
                # An algorithm that is not a chat-completions peer (e.g.
                # workspace_agent, which needs run_id + park) cannot serve this
                # surface. Reject loudly rather than dispatch it into a context
                # it will fail in.
                return JSONResponse(
                    status_code=400,
                    content={"error": {
                        "message": (
                            f"mode='{resolved.mode}' does not support stream=true"
                        ),
                        "type": "invalid_request_error",
                    }},
                )
            run_request = RunRequest(
                mode=resolved.mode,
                question=question,
                history=history,
                messages=messages,
                agent_overrides=resolved.agent_overrides,
                knowledge_filters=resolved.knowledge_filters,
                ui_language=ui_language,
            )
            cancel_event = threading.Event()
            return StreamingResponse(
                guarded_stream(
                    question,
                    history,
                    sem,
                    algorithm=algorithm,
                    runtime=container.runtime,
                    run_request=run_request,
                    providers=resolved.providers,
                    strategies=resolved.strategies,
                    settings=chat_agent_settings,
                    lanes=lanes,
                    include_progress=include_progress,
                    request=req,
                    cancel_event=cancel_event,
                    quota_service=quota_service,
                    principal=principal,
                    audit_sink=container.permission_service.audit_sink,
                    audit_service_starts=(
                        settings.observability.audit_service_starts
                    ),
                    incognito=incognito,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # The non-streaming transport gets the same disconnect semantics
        # as SSE: a client abort flips the cancel event, the algorithm
        # stops at its next checkpoint/probe, and the (undeliverable)
        # response is discarded instead of burning provider budget.
        async with disconnect_watch(req) as cancel_event:
            response = await chat_service.complete(
                question=question,
                history=history,
                messages=messages,
                resolved=resolved,
                chat_agent_settings=chat_agent_settings,
                semaphore=sem,
                lanes=lanes,
                principal=principal,
                cancel_event=cancel_event,
                incognito=incognito,
                ui_language=ui_language,
            )
        # Book real token spend on success and on a typed returned terminal
        # failure.  Ordinary pre-execution error envelopes carry no usage;
        # ChatService attaches a private usage projection only when an
        # algorithm actually consumed tokens before failing closed.
        usage_payload = (
            response
            if isinstance(response, dict)
            else getattr(response, "inqtrix_usage", None)
        )
        if isinstance(usage_payload, dict):
            await quota_record(
                quota_service,
                principal,
                QuotaDimension.LLM_TOKENS,
                consumed_tokens(usage_payload.get("usage", usage_payload)),
            )
        from inqtrix.services.audit_service import audit_chat_completed

        await audit_chat_completed(
            container.permission_service.audit_sink,
            principal,
            usage=(
                usage_payload.get("usage", usage_payload)
                if isinstance(usage_payload, dict)
                else None
            ),
            streamed=False,
            failed=not isinstance(response, dict),
            enabled=settings.observability.audit_service_starts,
        )
        return response

    return router
