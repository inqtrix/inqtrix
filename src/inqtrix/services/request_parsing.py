"""Protocol-level request parsing shared by the HTTP routers.

History formatting, OpenAI-content flattening, question/messages
normalization with payload caps, the project namespace of reads and
writes, and the OpenAI-style error envelope. Status codes, English diagnostics
and cap calculations are covered by ``tests/contract``.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from inqtrix.constants import REQUEST_WAIT_MARGIN_SECONDS
from inqtrix.settings import AgentSettings, ServerSettings

if TYPE_CHECKING:
    from inqtrix.auth.directory import UserDirectory
    from inqtrix.auth.principal import Principal

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


HISTORY_CHAR_BUDGET = 32_000
"""Character budget of the whole client-supplied history block (about 8,000
tokens)."""

USER_TURN_OPENING_CHARS = 500
"""What every earlier user turn keeps before newer messages are spent: an
instruction the user gave early ("always answer in English") stays in force
however long the answers after it grow."""

_MESSAGE_CUT_MARKER = " [... message shortened]"
_USER_LABEL = "Nutzer"
_ROLE_LABELS = {
    "user": _USER_LABEL,
    "assistant": "Assistent",
    "system": "System",
    "developer": "System",
}
# Any other role (a tool result, a function answer) is no text of the user's.
_OTHER_ROLE_LABEL = "Tool"


def _omitted_line(count: int) -> str:
    noun = "message" if count == 1 else "messages"
    return f"[... {count} earlier {noun} omitted]"


def format_history(messages: list[dict], max_messages: int = 20) -> str:
    """Format message history for agent context, with VISIBLE cuts.

    The budget keeps a client-supplied transcript from crowding out the
    task. Every user turn first keeps its opening, so an instruction the user
    gave early stays in force; the rest of the budget is spent from the
    newest message backwards, so the answer a follow-up asks about arrives
    whole: a fixed cut of every message leaves a follow-up with the opening
    of a long answer, and the model answers that the rest cannot be checked.
    No cut is silent: a shortened message says so, and every run of left-out
    messages, those beyond the window of *max_messages* included, stands as
    one line in its place, because the caller assumes everything it sent is
    in force.
    """
    if len(messages) <= 1:
        return ""
    entries: list[tuple[str, str]] = []
    for msg in messages[:-1]:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if content:
            role = msg.get("role", "user")
            entries.append((_ROLE_LABELS.get(role, _OTHER_ROLE_LABEL), content))
    window = entries[-max_messages:] if max_messages > 0 else []
    beyond_window = len(entries) - len(window)

    # The openings of the user turns, newest first, within the budget.
    floors: dict[int, int] = {}
    remaining = HISTORY_CHAR_BUDGET
    for index in reversed(range(len(window))):
        label, content = window[index]
        if label == _USER_LABEL:
            floors[index] = min(len(content), USER_TURN_OPENING_CHARS, remaining)
            remaining -= floors[index]

    kept: list[str | None] = [None] * len(window)
    for index in reversed(range(len(window))):
        label, content = window[index]
        floor = floors.get(index, 0)
        if len(content) - floor <= remaining:
            kept[index] = f"{label}: {content}"
            remaining -= len(content) - floor
        elif floor + remaining > 0:
            kept[index] = f"{label}: {content[:floor + remaining].rstrip()}{_MESSAGE_CUT_MARKER}"
            remaining = 0

    parts: list[str] = []
    left_out = beyond_window
    for line in kept:
        if line is None:
            left_out += 1
            continue
        if left_out:
            parts.append(_omitted_line(left_out))
            left_out = 0
        parts.append(line)
    if left_out:
        parts.append(_omitted_line(left_out))
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
    """Resolve and validate the optional project namespace a request names.

    This value is a CLIENT-SUPPLIED namespace: a read uses it to filter
    listings and stays unscoped without it. It is not, and must never
    become, an authorization input. The authorization workspace (a
    server-side membership fact) is a separate concept resolved from the
    verified principal. Writes resolve their namespace through
    ``workspace_id_for_write``.
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


async def workspace_id_for_write(
    req: Request,
    principal: "Principal",
    users: "UserDirectory | None",
    body: dict[str, Any] | None = None,
) -> str | None:
    """The project namespace a new row is stored with.

    An explicit namespace (header, query or body) wins and is validated as
    for reads. Without one, a signed-in user writes into their canonical
    project namespace, claimed once through the user directory when none
    exists yet, the same claim a first session start makes; so a row a script
    writes with a personal access token appears in that person's UI. A
    principal without a user (modes ``apikey`` and ``none``) keeps no
    namespace. Reads without a namespace stay unscoped: they use
    ``workspace_id_from_request``.
    """
    explicit = workspace_id_from_request(req, body)
    if explicit is not None:
        return explicit
    if principal.user_id is None or users is None:
        return None
    return await users.resolve_default_workspace(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        candidate=f"ws_{uuid.uuid4().hex}",
    )


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
