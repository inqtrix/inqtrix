"""SSE streaming utilities for OpenAI-compatible chat completion responses."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager, suppress
from functools import partial
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import Request

from inqtrix.core.constants import MODEL_NAME
from inqtrix.core.context import RunContext
from inqtrix.i18n import detect_ui_language, t
from inqtrix.providers.base import ProviderContext
from inqtrix.quota.models import QuotaDimension, consumed_tokens
from inqtrix.services.request_parsing import request_timeout_seconds
from inqtrix.settings import AgentSettings
from inqtrix.strategies import StrategyContext
from inqtrix.text import iter_word_chunks
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuditSink
    from inqtrix.auth.principal import Principal
    from inqtrix.core.algorithms import AgentAlgorithm
    from inqtrix.core.context import RuntimeContext
    from inqtrix.core.results import RunRequest
    from inqtrix.services.quota_service import QuotaService
    from inqtrix.server.execution import ExecutionLanes

log = logging.getLogger("inqtrix")



def make_chunk(
    chat_id: str,
    content: str,
    finish_reason: str | None = None,
    *,
    inqtrix: dict[str, Any] | None = None,
) -> str:
    """Build a single SSE chunk in the OpenAI streaming format."""
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content} if content else {},
                "finish_reason": finish_reason,
            }
        ],
    }
    if inqtrix:
        chunk["inqtrix"] = inqtrix
    return f"data: {json.dumps(chunk)}\n\n"


async def watch_disconnect(
    request: Request,
    cancel_event: threading.Event,
) -> None:
    """Background task: signal *cancel_event* on the next ``http.disconnect``.

    Why this is a dedicated task rather than a poll inside the streaming
    loop: ``Request.is_disconnected()`` is a non-blocking probe that uses
    an ``anyio.CancelScope().cancel()`` trick to read the ASGI receive
    channel without blocking. uvicorn does not deliver ``http.disconnect``
    via that path during an active streaming response (its ASGI receive
    side stays idle while we only write SSE chunks), so the probe never
    flips. A blocking ``await request.receive()`` in a parallel task is
    the only reliable way to surface the disconnect. Polling alone can
    miss it and allow a long-running request to complete after the client
    has already aborted.

    The mechanism is transport-generic: after a JSON route has consumed
    its request body, the next ``receive()`` parks the same way until
    connection loss, so non-streaming handlers use this watcher too.

    Args:
        request: The Starlette/FastAPI request whose receive channel we
            listen on. Must be the live request object the route handler
            saw — passing a stale or replayed request silently no-ops.
        cancel_event: Threading event the LangGraph node-boundary probe
            (``inqtrix.state.check_cancel_event``) consults. We set it
            on the first ``http.disconnect`` message; subsequent messages
            are ignored. Setting is thread-safe (used by both the asyncio
            loop here and the agent ThreadPoolExecutor that reads it).

    Notes:
        * The task exits as soon as the disconnect is observed. The
          caller — normally :func:`disconnect_watch`, or the streaming
          generator's own shutdown — is responsible for cancelling this
          task on normal completion (``task.cancel()`` + ``await task``
          with ``CancelledError`` swallowed) so it does not leak.
        * Any exception inside ``request.receive()`` (e.g. underlying
          transport torn down by uvicorn) also signals the cancel — we
          treat unexpected receive errors as "client gone" rather than
          continuing without the disconnect signal.
    """
    try:
        while not cancel_event.is_set():
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                cancel_event.set()
                log.info("Run cancelled by client disconnect")
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — disconnect watcher must not crash the stream
        log.debug(
            "Disconnect watcher exiting after receive() error "
            "(error_type=%s)",
            type(exc).__name__,
        )
        cancel_event.set()


@asynccontextmanager
async def disconnect_watch(request: Any) -> "AsyncIterator[threading.Event]":
    """Run :func:`watch_disconnect` for the enclosed block.

    Yields the cancel event the watcher flips on client disconnect. The
    watcher task is cancelled and awaited on EVERY exit path — routes may
    return from inside the block without leaking the task. A request
    object without a ``receive`` channel (library smoke tests) yields an
    event nobody ever sets.
    """
    cancel_event = threading.Event()
    watcher: "asyncio.Task | None" = None
    if request is not None and hasattr(request, "receive"):
        watcher = asyncio.create_task(watch_disconnect(request, cancel_event))
    try:
        yield cancel_event
    finally:
        if watcher is not None and not watcher.done():
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


async def stream_response(
    question: str,
    *,
    algorithm: "AgentAlgorithm",
    runtime: "RuntimeContext",
    run_request: "RunRequest",
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
    lanes: "ExecutionLanes",
    include_progress: bool = True,
    request: Request | None = None,
    cancel_event: threading.Event | None = None,
    quota_service: "QuotaService | None" = None,
    principal: "Principal | None" = None,
    audit_sink: "AuditSink | None" = None,
    audit_service_starts: bool = True,
    incognito: bool = False,
) -> AsyncIterator[str]:
    """Execute the agent and yield progress updates + answer as SSE chunks.

    The algorithm is dispatched through the registry (``algorithm.run`` with a
    per-request :class:`~inqtrix.core.context.RunContext`), exactly like the
    non-streamed chat path and native ``/v1/runs`` — there is no separate
    graph binding for streaming. Graph-backed modes (research, direct_llm)
    stream their coarse progress through the ``progress_queue``; event-only
    modes (knowledge) carry no
    ``event_sink`` here and stream the answer without granular progress lines
    (the rich progress surface for those is native ``/v1/runs`` SSE).

    When ``request`` is supplied (server route path), a background
    :func:`watch_disconnect` task is spawned. It blocks on
    ``await request.receive()`` and sets ``cancel_event`` as soon as
    uvicorn delivers ``http.disconnect`` — the only ASGI signal that
    actually fires when an SSE client closes the socket mid-stream.
    The streaming loop only checks ``cancel_event`` (no own polling)
    and exits the generator when set.

    ``cancel_event`` defaults to a fresh :class:`threading.Event` when
    not supplied so library tests can still inspect the sequence
    without wiring a Request object. Backwards compatibility: tests
    that supply a Request mock with ``is_disconnected()`` continue to
    pass because the watcher only depends on ``await request.receive()``
    — they just need to expose ``receive()`` as well.

    ``incognito`` keeps the turn's content out of every span it records.
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    request_deadline = time.monotonic() + request_timeout_seconds(settings)
    if cancel_event is None:
        cancel_event = threading.Event()

    # The agent runs in a worker thread, so its mutated state["language"] is
    # not visible from this generator. Pre-compute a UI-language pseudo-state
    # for SSE error chunks emitted from this side.
    _ui_lang = detect_ui_language(question)
    ui_state: dict[str, Any] = {"language": _ui_lang}

    # OpenAI-compatible first chunk: role announcement
    role_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
        ],
    }
    yield f"data: {json.dumps(role_chunk)}\n\n"

    # Progress queue for live updates from the agent
    progress_queue: Queue | None = Queue() if include_progress else None
    loop = asyncio.get_running_loop()

    # The per-request execution bundle, identical in spirit to the native-run
    # RunContext (run_service.py). event_sink stays None: the chat SSE surface
    # renders coarse progress from the queue only. run_id/park are None (chat
    # cannot park — a parking algorithm fails loudly), token_budget 0 (no cap).
    run_context = RunContext(
        providers=providers,
        strategies=strategies,
        agent_settings=settings,
        principal=principal,
        run_id=None,
        cancel_token=cancel_event,
        event_sink=None,
        progress_queue=progress_queue,
        token_budget=0,
        park=None,
    )

    # Start the algorithm in a separate thread, dispatched through the
    # registry. The root span opens INSIDE the thread (run_in_executor
    # copies no contextvars) — this closes the historical tracing
    # blind spot of the OpenAI-compatible streaming path.
    from inqtrix.observability.otel import chat_thread_call

    agent_future = loop.run_in_executor(
        lanes.ai,
        chat_thread_call(
            partial(
                algorithm.run,
                run_request,
                runtime=runtime,
                context=run_context,
            ),
            mode=run_request.mode,
            principal=principal,
            streamed=True,
            incognito=incognito,
        ),
    )

    # Spawn the disconnect watcher when we have a real request. Tests
    # that pass request=None (library smoke tests) bypass this path.
    disconnect_watcher: asyncio.Task | None = None
    if request is not None and hasattr(request, "receive"):
        disconnect_watcher = asyncio.create_task(
            watch_disconnect(request, cancel_event),
        )

    async def _shutdown_watcher() -> None:
        """Cancel and await the watcher task, swallowing the expected CancelledError."""
        if disconnect_watcher is None or disconnect_watcher.done():
            return
        disconnect_watcher.cancel()
        try:
            await disconnect_watcher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — cleanup-only
            pass

    async def _audit_terminal(reason: str) -> None:
        """Index row for an exit that never reaches the normal path.

        Timeouts and agent errors return before the completion row is
        written; without this the admin index would show no trace of a
        request that definitely ran. Usage is unknown on these paths
        (the agent thread may still be running), so the row carries the
        reason instead of token counts.
        """
        from inqtrix.services.audit_service import audit_chat_completed

        await audit_chat_completed(
            audit_sink,
            principal,
            usage=None,
            streamed=True,
            failed=True,
            enabled=audit_service_starts,
            reason=reason,
        )

    # Read progress updates and stream them as SSE chunks. Track whether ANY
    # progress chunk actually streamed, so the answer/progress separator is
    # emitted only when there was a progress section to separate from (event-only
    # modes emit no queue progress -> their stream stays answer-only).
    progress_emitted = False
    while include_progress and progress_queue is not None and not agent_future.done():
        if time.monotonic() >= request_deadline:
            await _shutdown_watcher()
            await _audit_terminal("request_deadline")
            yield make_chunk(chat_id, t(ui_state, "sse_request_timeout"))
            yield make_chunk(chat_id, "", finish_reason="stop")
            yield "data: [DONE]\n\n"
            return
        if cancel_event.is_set():
            await _shutdown_watcher()
            return
        try:
            msg_type, msg_content = await loop.run_in_executor(
                lanes.streams, partial(progress_queue.get, True, 0.3),
            )
            if msg_type == "progress" and msg_content != "done":
                progress_emitted = True
                yield make_chunk(chat_id, f"> `{msg_content}`\n>\n")
        except Empty:
            continue
        except Exception as exc:
            log.warning(
                "Progress streaming disabled after an unexpected error (error_type=%s)",
                type(exc).__name__,
            )
            break

    # Agent finished -- drain remaining queue messages
    while include_progress and progress_queue is not None and not progress_queue.empty():
        try:
            msg_type, msg_content = progress_queue.get_nowait()
            if msg_type == "progress" and msg_content != "done":
                progress_emitted = True
                yield make_chunk(chat_id, f"> `{msg_content}`\n>\n")
        except Empty:
            break
        except Exception as exc:
            log.warning(
                "Remaining progress messages could not be serialized (error_type=%s).",
                type(exc).__name__,
            )
            break

    # Get the result
    try:
        remaining = max(0.0, request_deadline - time.monotonic())
        if remaining <= 0.0:
            raise asyncio.TimeoutError
        agent_result = await asyncio.wait_for(agent_future, timeout=remaining)
        result = agent_result.raw
        answer_text = agent_result.answer
    except asyncio.TimeoutError:
        await _shutdown_watcher()
        # The agent thread keeps running to completion (it cannot be
        # cancelled mid-call), but its usage is never read here, so the
        # spend goes unbilled. Make that visible rather than silent
        # — only relevant when metering is active.
        if quota_service is not None:
            log.warning(
                "Streamed run timed out; abandoned token spend not "
                "booked toward quota."
            )
        await _audit_terminal("timeout")
        yield make_chunk(chat_id, t(ui_state, "sse_request_timeout"))
        yield make_chunk(chat_id, "", finish_reason="stop")
        yield "data: [DONE]\n\n"
        return
    except Exception as e:
        await _shutdown_watcher()
        log.error("Agent error (error_type=%s)", type(e).__name__)
        await _audit_terminal(f"agent_error:{type(e).__name__}")
        yield make_chunk(
            chat_id,
            t(ui_state, "sse_agent_error", err=sanitize_error(e)),
        )
        yield make_chunk(chat_id, "", finish_reason="stop")
        yield "data: [DONE]\n\n"
        return

    # Book the real token spend now that the agent has finished —
    # before the cancel short-circuit below, so a run the client
    # abandoned still counts what it consumed toward the monthly quota.
    # Recording is non-fatal (the service swallows store errors), so it
    # never breaks the stream.
    if quota_service is not None:
        await quota_service.record(
            principal,
            QuotaDimension.LLM_TOKENS,
            consumed_tokens(result.get("usage")),
        )

    # Service-start index row — right beside the quota booking so an
    # abandoned stream that consumed tokens still leaves its entry.
    from inqtrix.services.audit_service import audit_chat_completed

    await audit_chat_completed(
        audit_sink,
        principal,
        usage=result.get("usage") or None,
        streamed=True,
        failed=agent_result.terminal_failure is not None,
        enabled=audit_service_starts,
    )

    terminal_failure = agent_result.terminal_failure
    if terminal_failure is not None:
        # A returned terminal failure preserves metering/audit information but
        # is not an answer.  Stream only its safe explanation and close with an
        # explicit error reason; never emit the rejected model completion as a
        # normal word stream followed by ``finish_reason=stop``.
        await _shutdown_watcher()
        yield make_chunk(
            chat_id,
            terminal_failure.message,
            inqtrix={
                "error": {
                    "type": terminal_failure.type,
                    "message": terminal_failure.message,
                }
            },
        )
        yield make_chunk(chat_id, "", finish_reason="error")
        yield "data: [DONE]\n\n"
        return

    # Cancel-on-disconnect path: graph.run returns cancelled=True with an
    # empty answer. Stop emitting because the client is gone.
    # Defense-in-depth: also short-circuit when the cancel_event is set
    # but the result-state lacks the cancelled marker (e.g. an agent that
    # finished one tick before the cancel probe could raise). This covers
    # the race where the watcher task fires after the agent already
    # returned naturally — semantically the client is still gone, so
    # streaming the answer would be wasted bandwidth.
    if (
        result.get("result_state", {}).get("cancelled")
        or cancel_event.is_set()
    ):
        await _shutdown_watcher()
        log.info("Run finished in cancelled state; stopping stream.")
        return

    # Separator between progress and answer — only when progress actually
    # streamed (event-only modes emit none, so their stream is answer-only).
    if progress_emitted:
        yield make_chunk(chat_id, "\n\n---\n\n")

    model_resolution = (
        result.get("result_state", {})
        .get("node_model_resolutions", {})
        .get("direct_chat")
    )
    if isinstance(model_resolution, dict):
        yield make_chunk(
            chat_id,
            "",
            inqtrix={"model_resolution": model_resolution},
        )

    # Stream answer word-by-word for better UX
    for token in iter_word_chunks(answer_text):
        yield make_chunk(chat_id, token)

    yield make_chunk(chat_id, "", finish_reason="stop")
    yield "data: [DONE]\n\n"
    # Normal completion: tear down the disconnect watcher so the
    # background task does not leak past the request lifecycle.
    await _shutdown_watcher()


async def guarded_stream(
    question: str,
    history: str,
    sem: asyncio.Semaphore,
    *,
    algorithm: "AgentAlgorithm",
    runtime: "RuntimeContext",
    run_request: "RunRequest",
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
    lanes: "ExecutionLanes",
    include_progress: bool = True,
    request: Request | None = None,
    cancel_event: threading.Event | None = None,
    quota_service: "QuotaService | None" = None,
    principal: "Principal | None" = None,
    audit_sink: "AuditSink | None" = None,
    audit_service_starts: bool = True,
    incognito: bool = False,
) -> AsyncIterator[str]:
    """Stream with semaphore guard for correct concurrency limiting.

    The semaphore is held INSIDE the generator so it is only released
    after the streaming is complete. ``request`` and ``cancel_event``
    are forwarded to :func:`stream_response` so the disconnect probe
    and the implicit cancel pathway take effect; ``quota_service`` and
    ``principal`` let the inner generator book the streamed run's token
    spend once it knows the usage. ``history`` stays a leading positional
    for the historical call shape; the algorithm reads it from
    ``run_request.history``.
    """
    async with sem:
        async for chunk in stream_response(
            question,
            algorithm=algorithm,
            runtime=runtime,
            run_request=run_request,
            providers=providers,
            strategies=strategies,
            settings=settings,
            lanes=lanes,
            include_progress=include_progress,
            request=request,
            cancel_event=cancel_event,
            quota_service=quota_service,
            principal=principal,
            audit_sink=audit_sink,
            audit_service_starts=audit_service_starts,
            incognito=incognito,
        ):
            yield chunk
