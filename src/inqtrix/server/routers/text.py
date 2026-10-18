"""Text-improvement endpoint (``/v1/text/improvements``).

One LLM call per request, no agent run. Body moved verbatim from the
monolithic route factory.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.principal import Principal
from inqtrix.observability.context import bound_thread_call
from inqtrix.quota.models import QuotaDimension
from inqtrix.server.routers import (
    quota_admission,
    quota_record,
    stack_error_response,
)
from inqtrix.server.text_improvements import (
    TextImprovementError,
    build_text_improvement_prompt,
    parse_text_improvement_payload,
    parse_text_improvement_response,
)
from inqtrix.services.agent_context import StackResolutionError
from inqtrix.services.request_parsing import (
    error_response,
    text_wait_seconds,
    workspace_id_from_request,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the text-improvement route against the container."""
    router = APIRouter()
    resolver = container.resolver
    quota_service = container.quota_service
    lanes = container.execution_lanes

    @router.post("/v1/text/improvements")
    async def improve_text(
        req: Request,
        principal: Principal = Depends(container.principal_dependency),
    ):
        """Improve one browser text field without creating an agent run."""
        try:
            body = await req.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")

        try:
            workspace_id_from_request(req, body)
            resolved = resolver.resolve(body)
            improvement_request = parse_text_improvement_payload(
                body,
                max_text_chars=resolved.agent_settings.max_question_length,
            )
        except ValueError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        except StackResolutionError as exc:
            return stack_error_response(exc)

        denied = await quota_admission(
            quota_service, principal, QuotaDimension.LLM_TOKENS
        )
        if denied is not None:
            return denied

        sem = container.semaphore_factory()
        if sem.locked():
            return error_response(
                429,
                "Too many concurrent requests. Please wait.",
                "rate_limit_error",
            )

        prompt = build_text_improvement_prompt(improvement_request)
        # state= captures the provider's REAL token usage (prompt +
        # completion, including reasoning tokens a char estimate misses);
        # track_tokens accumulates with +=, so the counters pre-exist.
        usage_state = {"total_prompt_tokens": 0, "total_completion_tokens": 0}
        async with sem:
            loop = asyncio.get_running_loop()
            try:
                raw_response = await asyncio.wait_for(
                    loop.run_in_executor(
                        lanes.ai,
                        bound_thread_call(
                            partial(
                                resolved.providers.llm.complete,
                                prompt,
                                max_output_tokens=2500,
                                timeout=resolved.agent_settings.claim_extract_timeout,
                                state=usage_state,
                            ),
                            feature="editor",
                            usage_subject=(
                                principal.tenant_id,
                                principal.user_id,
                                None,
                            ),
                        ),
                    ),
                    timeout=text_wait_seconds(resolved.agent_settings),
                )
                result = parse_text_improvement_response(raw_response)
            except asyncio.TimeoutError:
                log.warning(
                    "Text improvement timed out (consider increasing "
                    "CLAIM_EXTRACT_TIMEOUT/MAX_TOTAL_SECONDS).",
                )
                return error_response(
                    504,
                    "Text improvement timed out",
                    "timeout_error",
                )
            except TextImprovementError as exc:
                log.warning(
                    "Text improvement could not be parsed (error_type=%s).",
                    type(exc).__name__,
                )
                return error_response(502, str(exc), "server_error")
            except Exception as exc:  # noqa: BLE001 — provider failures map to 502
                log.error(
                    "Text improvement error (error_type=%s)",
                    type(exc).__name__,
                )
                return error_response(
                    502,
                    f'Text improvement error: {sanitize_error(exc)}',
                    "server_error",
                )

        await quota_record(
            quota_service,
            principal,
            QuotaDimension.LLM_TOKENS,
            int(usage_state["total_prompt_tokens"])
            + int(usage_state["total_completion_tokens"]),
        )
        return result.to_payload()

    return router
