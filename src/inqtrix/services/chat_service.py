"""Non-streaming chat-completion execution via the algorithm registry.

The streaming path stays in :mod:`inqtrix.server.streaming` (its
generator owns the SSE lifecycle, disconnect watcher, and semaphore
hold); this service owns the request/response path. Wire payloads are
built from the algorithm's raw result dict — contract-locked by
``tests/contract/test_chat_contract.py``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from functools import partial
from typing import TYPE_CHECKING, Any

from fastapi.responses import JSONResponse

from inqtrix.core.constants import MODEL_NAME
from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded
from inqtrix.services.agent_context import ResolvedAgentContext
from inqtrix.services.request_parsing import request_timeout_seconds
from inqtrix.settings import AgentSettings
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal
    from inqtrix.core.algorithms import AlgorithmRegistry
    from inqtrix.server.execution import ExecutionLanes

log = logging.getLogger("inqtrix")



class ChatService:
    """Execute non-streaming chat completions through the registry.

    Args:
        registry: The algorithm registry; the resolved request mode
            selects the algorithm.
        runtime: App-level runtime context handed through to the
            algorithm.
    """

    def __init__(
        self,
        *,
        registry: "AlgorithmRegistry",
        runtime: RuntimeContext,
    ) -> None:
        self._registry = registry
        self._runtime = runtime

    async def complete(
        self,
        *,
        question: str,
        history: str,
        messages: list[dict[str, Any]],
        resolved: ResolvedAgentContext,
        chat_agent_settings: AgentSettings,
        semaphore: asyncio.Semaphore,
        lanes: "ExecutionLanes",
        principal: "Principal | None" = None,
        cancel_event: "threading.Event | None" = None,
        incognito: bool = False,
        ui_language: str = "",
    ) -> JSONResponse | dict[str, Any]:
        """Run the agent and build the OpenAI-compatible payload.

        Args:
            question: Normalized current user question.
            history: Pre-formatted conversation history block.
            messages: Raw chat messages (carried on the run request).
            resolved: Stack/override/mode resolution for this request.
            chat_agent_settings: Settings after the direct-chat
                question-length adjustment.
            lanes: Named thread lanes; the AI call runs on the lane
                sized to match *semaphore*, so an admitted request never
                waits for a thread.
            semaphore: The shared concurrency limiter; held for the
                duration of the agent execution.
            principal: Verified request identity, threaded into the
                run context for attribution.
            cancel_event: Optional client-disconnect signal (the route
                owns the watcher). The algorithm observes it at its
                checkpoints and provider probes; an aborted request
                answers 499 — visibly, though nobody receives it.
            incognito: Keep the turn's content out of every span it
                records (metadata, usage and codes only).
            ui_language: Interface language of a knowledge ask, the
                answer language where the question leaves it open.

        Returns:
            The chat-completion payload dict on success, or a
            :class:`JSONResponse` carrying the historical error
            envelope on timeout/agent failure/client abort.
        """
        algorithm = self._registry.get(resolved.mode)
        run_request = RunRequest(
            mode=resolved.mode,
            question=question,
            history=history,
            messages=messages,
            agent_overrides=resolved.agent_overrides,
            knowledge_filters=resolved.knowledge_filters,
            ui_language=ui_language,
        )
        run_context = RunContext(
            providers=resolved.providers,
            strategies=resolved.strategies,
            agent_settings=chat_agent_settings,
            principal=principal,
            cancel_token=cancel_event,
        )

        async with semaphore:
            loop = asyncio.get_running_loop()
            # Root span for the chat request, opened INSIDE the executor
            # thread (run_in_executor copies no contextvars) so every
            # provider span of this turn nests under it. Shared with the
            # OpenAI-compatible streaming mouth — ONE definition.
            from inqtrix.observability.otel import chat_thread_call

            try:
                agent_result = await asyncio.wait_for(
                    loop.run_in_executor(
                        lanes.ai,
                        chat_thread_call(
                            partial(
                                algorithm.run,
                                run_request,
                                runtime=self._runtime,
                                context=run_context,
                            ),
                            mode=resolved.mode,
                            principal=principal,
                            incognito=incognito,
                        ),
                    ),
                    timeout=request_timeout_seconds(chat_agent_settings),
                )
            except asyncio.TimeoutError:
                return JSONResponse(
                    status_code=504,
                    content={"error": {
                        "message": "Recherche-Request Timeout",
                        "type": "timeout_error",
                    }},
                )
            except AgentTokenBudgetExceeded as exc:
                # Subclass of AgentCancelled with a DIFFERENT meaning: a
                # resource stop, never a client disconnect. Its usage
                # snapshot keeps quota truthful.
                response = JSONResponse(
                    status_code=502,
                    content={"error": {
                        "message": sanitize_error(exc),
                        "type": "token_budget_exceeded",
                    }},
                )
                response.inqtrix_usage = dict(exc.usage or {})  # type: ignore[attr-defined]
                return response
            except AgentCancelled as exc:
                if cancel_event is None or not cancel_event.is_set():
                    # No disconnect happened — an AgentCancelled from
                    # another source must not masquerade as one.
                    log.error(
                        "Agent error (error_type=%s)",
                        type(exc).__name__,
                    )
                    return JSONResponse(
                        status_code=502,
                        content={"error": {
                            "message": f'Agent error: {sanitize_error(exc)}',
                            "type": "server_error",
                        }},
                    )
                log.warning(
                    "Chat request cancelled after client disconnect; response "
                    "discarded. Tokens already consumed by this request are not charged"
                    " to the quota; the usage ledger records them."
                )
                return JSONResponse(
                    status_code=499,
                    content={"error": {
                        "message": "The client closed the connection.",
                        "type": "client_closed_request",
                    }},
                )
            except Exception as exc:  # noqa: BLE001 — agent failures map to 502
                log.error(
                    "Agent error (error_type=%s)",
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=502,
                    content={"error": {
                        "message": f'Agent error: {sanitize_error(exc)}',
                        "type": "server_error",
                    }},
                )

            result = agent_result.raw
            usage = result.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            result_state = result.get("result_state", {}) or {}
            if agent_result.cancelled:
                # Graph modes return a cancelled result (with real usage)
                # instead of raising. Nobody receives this response —
                # the 499 exists so logs and quota stay truthful.
                response = JSONResponse(
                    status_code=499,
                    content={"error": {
                        "message": "The client closed the connection.",
                        "type": "client_closed_request",
                    }},
                )
                response.inqtrix_usage = dict(usage)  # type: ignore[attr-defined]
                return response
            terminal_failure = agent_result.terminal_failure
            if terminal_failure is not None:
                # The algorithm deliberately returned usage plus a safe
                # diagnostic, not a successful completion.  Do not package it
                # as choices[0]/finish_reason=stop: that would erase the shared
                # native-run failure contract on the chat surface.
                response = JSONResponse(
                    status_code=502,
                    content={
                        "error": {
                            "message": terminal_failure.message,
                            "type": terminal_failure.type,
                        }
                    },
                )
                # The route owns quota recording for non-streaming calls.  A
                # typed private projection preserves consumed usage without
                # leaking it into the public error envelope.
                response.inqtrix_usage = dict(usage)  # type: ignore[attr-defined]
                return response
            model_resolution = (
                result_state
                .get("node_model_resolutions", {})
                .get("direct_chat")
            )
            payload: dict[str, Any] = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_NAME,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": result["answer"],
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            }
            if isinstance(model_resolution, dict):
                payload["inqtrix"] = {"model_resolution": model_resolution}
            return payload
