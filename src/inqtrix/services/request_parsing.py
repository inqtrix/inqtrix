"""Protocol-level request parsing shared by the HTTP routers.

History formatting, OpenAI-content flattening, question/messages
normalization with payload caps, the client workspace namespace parser,
and the OpenAI-style error envelope. Status codes, English diagnostics
and cap calculations are covered by ``tests/contract``.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from inqtrix.constants import REQUEST_WAIT_MARGIN_SECONDS
from inqtrix.settings import AgentSettings, ServerSettings

WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
WORKSPACE_ID_HEADER = "x-inqtrix-workspace-id"


def error_response(
    status_code: int,
    message: str,
    error_type: str,
    **extra: Any,
) -> JSONResponse:
    """Return the OpenAI-style error envelope."""
    error = {"message": message, "type": error_type}
    error.update(extra)
    return JSONResponse(status_code=status_code, content={"error": error})


HISTORY_MESSAGE_CHAR_LIMIT = 500
"""Per-message bound of the client-supplied history block."""

_MESSAGE_CUT_MARKER = " [... message shortened]"


def format_history(messages: list[dict], max_messages: int = 20) -> str:
    """Format message history for agent context, with VISIBLE cuts.

    The bound keeps a client-supplied transcript from crowding out the
    task. It is not silent: a shortened message says so, because the
    caller assumes everything it sent is in force -- a standing
    requirement tends to sit at the END of a long message, which is
    exactly the part a fixed cut drops.
    """
    if len(messages) <= 1:
        return ""
    history_msgs = messages[:-1][-max_messages:]
    parts: list[str] = []
    for msg in history_msgs:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if content:
            label = {"user": "Nutzer", "assistant": "Assistent",
                     "system": "System"}.get(role, role)
            if len(content) > HISTORY_MESSAGE_CHAR_LIMIT:
                content = (
                    content[:HISTORY_MESSAGE_CHAR_LIMIT].rstrip()
                    + _MESSAGE_CUT_MARKER
                )
            parts.append(f"{label}: {content}")
    return "\n".join(parts)


def text_from_content(content: Any) -> str:
    """Extract user-visible text from OpenAI chat content."""
    if isinstance(content, list):
        return " ".join(
            p.get("text", "")
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content or "")


def workspace_id_from_request(
    req: Request,
    body: dict[str, Any] | None = None,
) -> str | None:
    """Resolve and validate the optional browser/project workspace namespace.

    This value is a CLIENT-SUPPLIED UI namespace used to filter run
    listings per browser profile — it is not, and must never become,
    an authorization input. The authorization workspace (a server-side
    membership fact) is a separate concept resolved from the verified
    principal.
    """
    body_workspace_id = body.get("workspace_id") if isinstance(body, dict) else None
    raw = (
        req.headers.get(WORKSPACE_ID_HEADER)
        or req.query_params.get("workspace_id")
        or body_workspace_id
    )
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or not WORKSPACE_ID_PATTERN.fullmatch(raw):
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "Invalid workspace_id.",
                "type": "invalid_request_error",
            }},
        )
    return raw


def incognito_from_request(body: dict[str, Any]) -> bool:
    """Whether the request asks that its content stays out of traces.

    Only a JSON boolean is accepted: a privacy request the server misread
    as absent would record what the caller asked to keep out.
    """
    raw = body.get("incognito", False)
    if not isinstance(raw, bool):
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "Field 'incognito' must be true or false.",
                "type": "invalid_request_error",
            }},
        )
    return raw


def ui_language_from_request(body: dict[str, Any], mode: str) -> str:
    """The interface language of a knowledge ask (``de``/``en``), or ``""``.

    It answers only a knowledge ask whose question leaves its language
    open, so it is refused anywhere else and in a language the interface
    does not have, never ignored.
    """
    raw = body.get("ui_language")
    if raw is None:
        return ""
    if raw not in ("de", "en"):
        message = "ui_language must be de or en."
    elif mode != "knowledge":
        message = "ui_language applies only to knowledge mode."
    else:
        return str(raw)
    raise HTTPException(
        status_code=400,
        detail={"error": {"message": message, "type": "invalid_request_error"}},
    )


def enforce_payload_caps(
    question: str,
    messages: list[dict[str, Any]],
    server: ServerSettings,
) -> None:
    """Reject oversize ``messages[]`` arrays with HTTP 413.

    Two caps from :class:`ServerSettings`:

    * ``max_message_count`` rejects array-bomb payloads that would
      stretch the agent's token bookkeeping for no real-user reason.
    * ``max_total_input_tokens`` rejects bodies whose combined
      *question + messages content* approximate-token count
      (4 chars per token) exceeds the operator-configured limit.
      Defends against pathological 1-message-with-megabytes payloads.

    The defaults are deliberately generous (200 messages, 500k
    tokens) so realistic multi-turn flows never trip them. The
    check runs after content normalization so it sees what the
    agent will actually consume.
    """
    max_count = server.max_message_count
    if len(messages) > max_count:
        raise HTTPException(
            status_code=413,
            detail={"error": {
                "message": (
                    f"messages array exceeds limit ({len(messages)} > "
                    f"{max_count})"
                ),
                "type": "payload_too_large",
            }},
        )
    total_chars = sum(
        len(text_from_content(msg.get("content", ""))) for msg in messages
    ) + len(question)
    approx_tokens = total_chars // 4
    max_tokens = server.max_total_input_tokens
    if approx_tokens > max_tokens:
        raise HTTPException(
            status_code=413,
            detail={"error": {
                "message": (
                    f"input size ~{approx_tokens} tokens exceeds limit "
                    f"({max_tokens})"
                ),
                "type": "payload_too_large",
            }},
        )


def question_and_messages(
    body: dict[str, Any],
    server: ServerSettings,
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve either native ``question`` or OpenAI ``messages`` input."""
    raw_question = body.get("question")
    messages = body.get("messages", [])
    if isinstance(raw_question, str) and raw_question.strip():
        question = raw_question.strip()
        normalized_messages = (
            messages
            if (
                isinstance(messages, list)
                and messages
                and all(isinstance(msg, dict) for msg in messages)
            )
            else [{"role": "user", "content": question}]
        )
        enforce_payload_caps(question, normalized_messages, server)
        return question, normalized_messages
    if not isinstance(messages, list) or not messages:
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "Field 'question' or non-empty 'messages' is required",
                "type": "invalid_request_error",
            }},
        )
    if not all(isinstance(msg, dict) for msg in messages):
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "messages must be a list of objects",
                "type": "invalid_request_error",
            }},
        )
    last = messages[-1]
    question = text_from_content(last.get("content", "")).strip()
    if not question:
        raise HTTPException(
            status_code=400,
            detail={"error": {
                "message": "Last message has no content",
                "type": "invalid_request_error",
            }},
        )
    enforce_payload_caps(question, messages, server)
    return question, messages


def request_timeout_seconds(agent_settings: AgentSettings) -> int:
    """HTTP-level deadline for a whole-run agent execution (graph cap + margin).

    Bounds the synchronous HTTP wait for a chat run and the SSE streaming
    deadline. Derives from ``max_total_seconds`` (the wall-clock run budget),
    so raising the run budget widens this wait in lockstep.
    """
    return agent_settings.max_total_seconds + REQUEST_WAIT_MARGIN_SECONDS


def editor_wait_seconds(agent_settings: AgentSettings) -> int:
    """HTTP wait for a single editor suggest/instruct call (seconds).

    Bounds the outer ``asyncio.wait_for`` around the editor LLM call, which
    itself runs under ``editor_assistant_timeout`` (the dedicated editor budget,
    decoupled from research ``reasoning_timeout``). Capped by the whole-run HTTP
    deadline so the editor wait can never outlive ``max_total_seconds``.
    """
    return min(
        request_timeout_seconds(agent_settings),
        agent_settings.editor_assistant_timeout + REQUEST_WAIT_MARGIN_SECONDS,
    )


def text_wait_seconds(agent_settings: AgentSettings) -> int:
    """HTTP wait for a single ``/v1/text`` improvement call (seconds).

    Bounds the outer ``asyncio.wait_for`` around the text-improvement LLM call,
    which runs under ``claim_extract_timeout``. Capped by the whole-run HTTP
    deadline. This is the path whose wait hangs off ``claim_extract_timeout``
    rather than the reasoning budget.
    """
    return min(
        request_timeout_seconds(agent_settings),
        agent_settings.claim_extract_timeout + REQUEST_WAIT_MARGIN_SECONDS,
    )
