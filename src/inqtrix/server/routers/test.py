"""Parity/testing endpoint (``/v1/test/run``), testing-mode only.

Calls the graph's test entry point through the
:mod:`inqtrix.research.web_research` seam so the module-global lookup
stays patchable like every other engine call.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

import inqtrix.research.web_research as web_research
from inqtrix.auth.principal import Principal
from inqtrix.server.routers import stack_error_response
from inqtrix.services.agent_context import StackResolutionError
from inqtrix.services.request_parsing import request_timeout_seconds
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the test-run route against the container."""
    router = APIRouter()
    settings = container.settings
    resolver = container.resolver

    @router.post("/v1/test/run")
    async def test_run(
        req: Request,
        principal: Principal = Depends(container.principal_dependency),
    ):
        """Run a single test question and return structured metrics."""
        if not settings.agent.testing_mode:
            return JSONResponse(
                status_code=404,
                content={"error": {
                    "message": "Test endpoint is available only in testing mode",
                    "type": "not_found",
                }},
            )

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

        question = body.get("question", "")
        if not question or not isinstance(question, str):
            return JSONResponse(
                status_code=400,
                content={"error": {
                    "message": "Field 'question' (string) is required",
                    "type": "invalid_request_error",
                }},
            )

        if len(question) > settings.agent.max_question_length:
            return JSONResponse(
                status_code=400,
                content={"error": {
                    "message": (
                        f'Question too long ({len(question)} characters, max. {settings.agent.max_question_length})'
                    ),
                    "type": "invalid_request_error",
                }},
            )

        try:
            resolved = resolver.resolve(body)
        except StackResolutionError as exc:
            return stack_error_response(exc)

        active_agent_settings = resolved.agent_settings

        loop = asyncio.get_running_loop()
        try:
            result = await asyncio.wait_for(
                # Deliberately the shared pool, not the AI lane: this
                # diagnostic route passes no admission gate, so counting it
                # against a lane sized from that gate would let it displace
                # the request path it exists to inspect.
                loop.run_in_executor(
                    None,
                    partial(
                        web_research.run_web_graph_test,
                        question,
                        providers=resolved.providers,
                        strategies=resolved.strategies,
                        settings=active_agent_settings,
                    ),
                ),
                timeout=request_timeout_seconds(active_agent_settings),
            )
        except asyncio.TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"error": {
                    "message": "Test run timed out",
                    "type": "timeout_error",
                }},
            )
        except Exception as exc:  # noqa: BLE001 — agent failures map to 502
            log.error(
                "Test run error (error_type=%s)",
                type(exc).__name__,
            )
            return JSONResponse(
                status_code=502,
                content={"error": {
                    "message": f'Agent error: {sanitize_error(exc)}',
                    "type": "server_error",
                }},
            )

        return result

    return router
