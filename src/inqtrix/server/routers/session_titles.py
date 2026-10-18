"""Optional conversation labels with scoped, revision-checked persistence."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from functools import partial
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inqtrix.auth.principal import Principal, UserContext
from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.observability import semconv
from inqtrix.observability.otel import (
    current_trace_id_hex,
    enrich_current_span,
    traced_thread_call,
)
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.project.chat_ports import ThreadNotFound
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.quota.models import QuotaDimension
from inqtrix.server.routers import quota_admission, stack_error_response
from inqtrix.services.agent_context import StackResolutionError
from inqtrix.services.request_parsing import error_response, workspace_id_from_request
from inqtrix.services.session_titler import (
    TITLE_TIMEOUT_SECONDS,
    TitleAdmission,
    generate_title,
    title_capability,
)
from inqtrix.services.workspace_guard import deny_cross_workspace

log = logging.getLogger("inqtrix")


class TitleTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["chat", "agent"]
    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0, strict=True)


class TitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1)
    language: str | None = None
    stack: str | None = None
    target: TitleTarget | None = None


def _title_payload(row):
    return {
        "title": row.title,
        "title_revision": row.title_revision,
        "title_source": row.title_source,
    }


def _reply(result):
    enrich_current_span({semconv.INQTRIX_OUTCOME: result.get("reason") or "generated"})
    return {**result, "trace_id": current_trace_id_hex()}


def build_router(container) -> APIRouter:
    router = APIRouter()
    admission = TitleAdmission()

    @router.post("/v1/session-titles")
    async def session_title(
        req: Request,
        principal: Principal = Depends(container.principal_dependency),
        visible_to: UserContext | None = Depends(container.user_context_dependency),
    ):
        enrich_current_span(
            {
                semconv.INQTRIX_NODE: "session_title",
                **(
                    {
                        semconv.LANGFUSE_USER_ID: stable_pseudonym(
                            "usr", principal.user_id
                        )
                    }
                    if principal.user_id is not None
                    else {}
                ),
            }
        )
        try:
            body = TitleRequest.model_validate(await req.json())
            workspace_id = workspace_id_from_request(req)
            resolved = container.resolver.resolve(body.model_dump(exclude_none=True))
        except (ValueError, ValidationError):
            return error_response(
                400, "Invalid session title request", "invalid_request_error"
            )
        except StackResolutionError as exc:
            return stack_error_response(exc)
        if (
            not body.question.strip()
            or len(body.question) > resolved.agent_settings.max_question_length
        ):
            return error_response(
                400,
                "Question is empty or exceeds the configured question limit",
                "invalid_request_error",
            )

        service = None
        current = None

        async def read_target():
            if body.target.kind == "chat":
                row = await service.get_thread(body.target.id, visible_to=visible_to)
                not_found = ThreadNotFound
            else:
                row = await service.get_session(body.target.id, visible_to=visible_to)
                not_found = AgentSessionNotFound
                if row.lifecycle_status != "active":
                    raise AgentSessionNotFound(body.target.id)
            deny_cross_workspace(
                resource_workspace_id=row.workspace_id,
                request_workspace_id=workspace_id,
                not_found=lambda: not_found(body.target.id),
            )
            return row

        if body.target:
            service = (
                container.chat_history_service
                if body.target.kind == "chat"
                else container.agent_sessions_service
            )
            if service is None:
                return error_response(404, "Conversation not found", "not_found")
            try:
                current = await read_target()
            except (ThreadNotFound, AgentSessionNotFound):
                return error_response(404, "Conversation not found", "not_found")
            if (
                current.title_source != "initial"
                or current.title_revision != body.target.revision
            ):
                return _reply(
                    {
                        "title": None,
                        "reason": "title_changed",
                        "current": _title_payload(current),
                    }
                )

        capability = title_capability(
            resolved.providers.llm,
            enabled=container.settings.server.session_titles_enabled,
        )
        if not capability["available"]:
            log.info("Session title skipped: %s", capability["reason"])
            return _reply(
                {
                    "title": None,
                    "reason": capability["reason"],
                    "model": capability["model"],
                }
            )
        denied = await quota_admission(
            container.quota_service, principal, QuotaDimension.LLM_TOKENS
        )
        if denied is not None:
            return denied
        key = (principal.tenant_id, principal.user_id, principal.kind)
        if not admission.acquire(key):
            log.info("Session title skipped: rate_limited")
            return _reply({"title": None, "reason": "rate_limited"})

        deadline = time.monotonic() + TITLE_TIMEOUT_SECONDS
        work = partial(
            generate_title,
            resolved.providers.llm,
            question=body.question,
            language=body.language,
            deadline=deadline,
            quota_service=container.quota_service,
            quota_subject=container.quota_service.subject_for(principal)
            if container.quota_service
            else None,
        )
        traced = traced_thread_call(
            "inqtrix.session_title",
            {semconv.INQTRIX_NODE: "session_title"},
            work,
            enrich=lambda span, value: span.set_attribute(
                semconv.INQTRIX_OUTCOME, value.get("reason") or "generated"
            ),
            feature="agent" if body.target and body.target.kind == "agent" else "chat",
            usage_subject=(principal.tenant_id, principal.user_id, workspace_id),
        )
        # Copy the request's trace context. Keep the admission permit until
        # the real worker finishes; an HTTP timeout cannot cancel a thread.
        context = contextvars.copy_context()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                container.execution_lanes.titles, context.run, traced
            )
        except Exception:
            admission.release()
            raise

        def finished(task):
            admission.release()
            if not task.cancelled():
                exception = task.exception()
                if exception is not None:
                    log.warning(
                        "Session title provider failed (error_type=%s)",
                        type(exception).__name__,
                    )

        future.add_done_callback(finished)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(future), timeout=TITLE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            log.info("Session title skipped: timeout")
            return _reply({"title": None, "reason": "timeout"})
        except Exception:
            return _reply({"title": None, "reason": "provider_error"})

        if result["title"] is not None and body.target:
            try:
                applied = await service.store.apply_generated_title(
                    body.target.id,
                    title=result["title"],
                    revision=body.target.revision,
                    scope=ResourceScope.from_record(current),
                )
                latest = await read_target()
            except (ThreadNotFound, AgentSessionNotFound):
                return _reply({**result, "title": None, "reason": "title_changed"})
            result["current"] = _title_payload(latest)
            if not applied:
                result.update(title=None, reason="title_changed")
        return _reply(result)

    return router
