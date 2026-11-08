"""The OpenAI Responses API for the models whose card names it, at the transport seam.

The Azure and LiteLLM adapters build Chat Completions requests and read Chat
Completions answers everywhere above their transport. For a model whose card says
``OpenAIRules(api="responses")`` (models that refuse function tools while they
reason over Chat Completions, or offer no Chat Completions at all), one attempt
turns that request into a Responses request and the Responses answer back into a
Chat Completions answer, so every caller and parser stays as it is.

Requests go out with ``store=False``: the provider keeps nothing. The answer's
output items (encrypted reasoning, messages with their ``phase``, function calls)
travel in order as the assistant message's provider state. The next turn of the
same model gets them back unchanged while the turn's text and calls are still
the ones the model wrote; otherwise the reasoning goes first and the text and
calls follow as they stand now, like the Anthropic adapter's thinking blocks.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import httpx
import openai
from openai.types.chat import ChatCompletion

from inqtrix.exceptions import AgentToolArgumentsError
from inqtrix.providers._openai_rules import openai_rules, warn_once
from inqtrix.providers.base import (
    PROVIDER_STATE_FIELD,
    ChatTurn,
    chat_turn_from_openai_response,
    is_foreign_state,
    optional_usage_count,
)

log = logging.getLogger(__name__)

OUTPUT_ITEMS_FIELD = "inqtrix_output_items"
"""Extra field of a converted Chat Completion: the answer's output items in order, JSON-plain."""

_TOKEN_BUDGET_KEYS = ("max_completion_tokens", "max_tokens")
_MAPPED_KEYS = frozenset({
    "messages", "tools", "tool_choice", "reasoning_effort", "response_format", "stream", "verbosity",
    "store", "include", "reasoning", *_TOKEN_BUDGET_KEYS,
})
_CHAT_ONLY_KEYS = frozenset({
    "audio", "frequency_penalty", "function_call", "functions", "logit_bias", "logprobs", "modalities", "n",
    "prediction", "presence_penalty", "seed", "stop", "stream_options", "web_search_options",
})
"""Chat Completions parameters the Responses API has no form for."""
_INPUT_ROLES = frozenset({"system", "developer", "user"})
_STATUS_ERRORS: dict[str, tuple[type[openai.APIStatusError], int]] = {
    "rate_limit_exceeded": (openai.RateLimitError, 429),
    "server_error": (openai.InternalServerError, 500),
    "vector_store_timeout": (openai.InternalServerError, 500),
}
_PART_SEPARATOR = "\n\n"


def uses_responses(model: str) -> bool:
    """Whether the calls of *model* go through the Responses API."""
    rules = openai_rules(model)
    return rules is not None and rules.api == "responses"


def api_name(model: str) -> str:
    """The API the calls of *model* go through, for log lines: ``responses`` or ``chat_completions``."""
    return "responses" if uses_responses(model) else "chat_completions"


def send(client: Any, create_kwargs: dict[str, Any], *, provider: str) -> Any:
    """The awaitable of one attempt: Chat Completions, or Responses where the card says so.

    Args:
        client: The adapter's ``AsyncOpenAI`` client.
        create_kwargs: The Chat Completions request of the attempt.
        provider: The adapter's name in provider state (``azure``, ``litellm``).

    Returns:
        An awaitable resolving to a Chat Completions answer.
    """
    if not uses_responses(str(create_kwargs.get("model") or "")):
        return client.chat.completions.create(**create_kwargs)
    return _responses_attempt(client, create_kwargs, provider=provider)


def sent_form(create_kwargs: dict[str, Any], *, provider: str) -> dict[str, Any]:
    """The request as it goes out, for size estimates: the Responses request where the card says so."""
    if not uses_responses(str(create_kwargs.get("model") or "")):
        return create_kwargs
    return responses_request(create_kwargs, provider=provider)


async def _responses_attempt(client: Any, create_kwargs: dict[str, Any], *, provider: str) -> ChatCompletion:
    response = await client.responses.create(**responses_request(create_kwargs, provider=provider))
    return chat_completion_from_response(response, url=f"{str(getattr(client, 'base_url', '') or '').rstrip('/')}/responses")


def responses_request(create_kwargs: dict[str, Any], *, provider: str) -> dict[str, Any]:
    """The Responses request that asks what the Chat Completions request *create_kwargs* asks.

    Parameters that both APIs take under the same name (``temperature``,
    ``timeout``, ``parallel_tool_calls``, ``top_p``, ``user``, ``metadata`` …)
    pass through unchanged; ``verbosity`` becomes ``text.verbosity``. A given
    ``include`` and ``reasoning`` are merged with what this path needs (the
    encrypted reasoning, the call's effort); ``store`` is always false here, and a
    request asking for ``store=True`` gets one ``CONFIG`` warning per model.

    Raises:
        ValueError: A Chat Completions-only parameter (``seed``, ``stop``, ``n`` …),
            named together with the model, before any request goes out.
    """
    model = str(create_kwargs.get("model") or "")
    chat_only = sorted(key for key in create_kwargs if key in _CHAT_ONLY_KEYS)
    if chat_only:
        raise ValueError(
            f"Model {model!r} runs on the Responses API, which has no form for the request parameter(s) "
            f"{', '.join(chat_only)}; remove them for this model."
        )
    request = {key: value for key, value in create_kwargs.items() if key not in _MAPPED_KEYS}
    request["input"] = responses_input(create_kwargs.get("messages") or [], provider=provider, model=model)
    if create_kwargs.get("store"):
        warn_once(
            "Responses", model,
            f"Model {model!r} runs on the Responses API without stored responses (store=false), so its "
            "reasoning can go back to the next turn; the request parameter store=true is not used.",
        )
    request["store"] = False
    include = [str(item) for item in create_kwargs.get("include") or []]
    request["include"] = [*include, *(["reasoning.encrypted_content"] if "reasoning.encrypted_content" not in include else [])]
    if create_kwargs.get("tools"):
        request["tools"] = [_function_tool(tool) for tool in create_kwargs["tools"]]
    if create_kwargs.get("tool_choice") is not None:
        request["tool_choice"] = _tool_choice(create_kwargs["tool_choice"])
    reasoning = dict(create_kwargs.get("reasoning") or {})
    if create_kwargs.get("reasoning_effort"):
        reasoning["effort"] = create_kwargs["reasoning_effort"]
    if reasoning:
        request["reasoning"] = reasoning
    budget = next((create_kwargs[key] for key in _TOKEN_BUDGET_KEYS if create_kwargs.get(key)), None)
    if budget:
        request["max_output_tokens"] = budget
    text: dict[str, Any] = {}
    if create_kwargs.get("response_format"):
        text["format"] = _text_format(create_kwargs["response_format"])
    if create_kwargs.get("verbosity"):
        text["verbosity"] = create_kwargs["verbosity"]
    if text:
        request["text"] = text
    return request


def responses_input(messages: list[dict[str, Any]], *, provider: str, model: str) -> list[dict[str, Any]]:
    """The Responses input items for chat *messages*, in order.

    A tool result becomes a ``function_call_output``. An assistant turn goes back
    as the model wrote it when its provider state is this provider's and this
    model's and the turn still has the text and calls of that answer; otherwise
    its own reasoning goes first, then its text (marked ``commentary`` before
    calls and ``final_answer`` without, where the model marks its messages),
    then its calls. Reasoning of another provider or model never goes back.

    Raises:
        ValueError: A message role or content part with no Responses form.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_call_id") or ""),
                "output": _text(message.get("content")),
            })
        elif role == "assistant":
            items.extend(_assistant_items(message, provider=provider, model=model))
        elif role in _INPUT_ROLES:
            items.append({"role": role, "content": _input_content(message.get("content"))})
        else:
            raise ValueError(f"The Responses API has no input form for a message with role {role!r}.")
    return items


def chat_completion_from_response(response: Any, *, url: str) -> ChatCompletion:
    """The Chat Completions answer that says what the Responses *response* says.

    A response that reports ``failed`` raises the SDK's status error for its code
    (rate limit, server error, bad request), so retries and error mapping treat it
    like the same failure over HTTP.

    Args:
        response: The Responses API answer.
        url: The request URL, for the error a failed response raises.
    """
    if getattr(response, "status", "") == "failed":
        raise _failed(response, url=url)
    texts: list[str] = []
    refusals: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in getattr(response, "output", None) or []:
        kind = getattr(item, "type", "")
        if kind == "message":
            text = "".join(
                str(getattr(part, "text", "") or "")
                for part in getattr(item, "content", None) or []
                if getattr(part, "type", "") == "output_text"
            )
            if text:
                texts.append(text)
            refusals.extend(
                str(getattr(part, "refusal", "") or "")
                for part in getattr(item, "content", None) or []
                if getattr(part, "type", "") == "refusal"
            )
        elif kind == "function_call":
            calls.append({
                "id": str(getattr(item, "call_id", "") or ""),
                "type": "function",
                "function": {"name": str(getattr(item, "name", "") or ""), "arguments": str(getattr(item, "arguments", "") or "")},
            })
    status = getattr(response, "status", "")
    reason = str(getattr(getattr(response, "incomplete_details", None), "reason", "") or "")
    if status == "incomplete" and reason == "max_output_tokens":
        finish = "length"
    elif status == "incomplete" and reason == "content_filter":
        finish = "content_filter"
    elif calls:
        finish = "tool_calls"
    else:
        finish = "stop"
    return ChatCompletion.model_validate({
        "id": str(getattr(response, "id", "") or ""),
        "object": "chat.completion",
        "created": int(getattr(response, "created_at", 0) or 0),
        "model": str(getattr(response, "model", "") or ""),
        "choices": [{
            "index": 0,
            "finish_reason": finish,
            "message": {
                "role": "assistant",
                "content": _PART_SEPARATOR.join(texts) or None,
                "refusal": "".join(refusals) or None,
                "tool_calls": calls or None,
            },
        }],
        "usage": _usage(getattr(response, "usage", None)),
        OUTPUT_ITEMS_FIELD: [
            item.model_dump(mode="json", exclude_none=True) for item in getattr(response, "output", None) or []
        ],
    })


def chat_turn(completion: Any, *, provider: str, model: str) -> ChatTurn:
    """The :class:`ChatTurn` of an OpenAI-compatible answer, with the output items it gives back.

    An answer this module converted carries its output items; when they hold
    something to give back (reasoning, or messages the model marked with a
    ``phase``) they become the turn's provider state. A rejected tool batch goes
    back to the model for a correction together with that state, so
    :class:`AgentToolArgumentsError` carries it too. Every other answer parses
    as before.
    """
    state = _state(completion, provider=provider, model=model)
    try:
        turn = chat_turn_from_openai_response(completion, model=model)
    except AgentToolArgumentsError as exc:
        if state is not None:
            exc.turn = dataclasses.replace(exc.turn, provider_state=state)
        raise
    return turn if state is None else dataclasses.replace(turn, provider_state=state)


def _state(completion: Any, *, provider: str, model: str) -> dict[str, Any] | None:
    items = getattr(completion, OUTPUT_ITEMS_FIELD, None)
    if not isinstance(items, list) or not _worth_returning(items):
        return None
    return {"provider": provider, "model": model, "content": json.loads(json.dumps(items))}


def _worth_returning(items: list[Any]) -> bool:
    return any(
        isinstance(item, dict)
        and (item.get("type") == "reasoning" or (item.get("type") == "message" and item.get("phase")))
        for item in items
    )


def _assistant_items(message: dict[str, Any], *, provider: str, model: str) -> list[dict[str, Any]]:
    text = _text(message.get("content"))
    calls = [
        {
            "type": "function_call",
            "call_id": str(call.get("id") or ""),
            "name": str((call.get("function") or {}).get("name") or ""),
            "arguments": _arguments((call.get("function") or {}).get("arguments")),
        }
        for call in message.get("tool_calls") or []
    ]
    stored = _own_output(message, provider=provider, model=model)
    if stored and _stored_text(stored) == text and _call_keys(stored) == _call_keys(calls):
        return stored
    out = [item for item in stored if item.get("type") == "reasoning"]
    if text:
        entry: dict[str, Any] = {"role": "assistant", "content": text}
        if any(item.get("type") == "message" and item.get("phase") for item in stored):
            entry["phase"] = "commentary" if calls else "final_answer"
        out.append(entry)
    out.extend(calls)
    return out


def _own_output(message: dict[str, Any], *, provider: str, model: str) -> list[dict[str, Any]]:
    state = message.get(PROVIDER_STATE_FIELD)
    if not isinstance(state, dict) or is_foreign_state(state, provider=provider):
        return []
    if state.get("model") != model:
        log.debug("Did not send the output of %s to %s: its reasoning belongs to the model that wrote it.", state.get("model"), model)
        return []
    return [item for item in state.get("content") or [] if isinstance(item, dict)]


def _stored_text(stored: list[dict[str, Any]]) -> str:
    texts = [
        "".join(str(part.get("text") or "") for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "output_text")
        for item in stored
        if item.get("type") == "message"
    ]
    return _PART_SEPARATOR.join(text for text in texts if text)


def _call_keys(items: list[dict[str, Any]]) -> list[tuple[str, str, Any]]:
    return [
        (str(item.get("call_id") or ""), str(item.get("name") or ""), _parsed(item.get("arguments")))
        for item in items
        if item.get("type") == "function_call"
    ]


def _parsed(arguments: Any) -> Any:
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def _usage(usage: Any) -> dict[str, Any] | None:
    """The Chat Completions usage block; a count the response does not report stays absent, not zero."""
    if usage is None:
        return None
    out: dict[str, Any] = {
        "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }
    cached = optional_usage_count(usage, "input_tokens_details", "cached_tokens")
    if cached is not None:
        out["prompt_tokens_details"] = {"cached_tokens": cached}
    reasoning = optional_usage_count(usage, "output_tokens_details", "reasoning_tokens")
    if reasoning is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    return out


def _failed(response: Any, *, url: str) -> openai.APIStatusError:
    error = getattr(response, "error", None)
    code = str(getattr(error, "code", "") or "")
    message = (
        f"The Responses call {getattr(response, 'id', '') or '(no id)'} failed ({code or 'no code'}): "
        f"{getattr(error, 'message', '') or 'no message'}"
    )
    kind, status = _STATUS_ERRORS.get(code, (openai.BadRequestError, 400))
    request = httpx.Request("POST", url)
    return kind(message, response=httpx.Response(status, request=request), body={"code": code, "message": message})


def _function_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function")
    if tool.get("type") != "function" or not isinstance(function, dict):
        return dict(tool)
    out = {"type": "function", "name": function.get("name"), "strict": bool(function.get("strict", False))}
    for key in ("description", "parameters"):
        if key in function:
            out[key] = function[key]
    return out


def _tool_choice(choice: Any) -> Any:
    if isinstance(choice, dict) and choice.get("type") == "function":
        return {"type": "function", "name": (choice.get("function") or {}).get("name") or choice.get("name")}
    return choice


def _text_format(response_format: dict[str, Any]) -> dict[str, Any]:
    if response_format.get("type") != "json_schema":
        return dict(response_format)
    schema = response_format.get("json_schema") or {}
    out = {"type": "json_schema", "name": schema.get("name"), "schema": schema.get("schema")}
    if "strict" in schema:
        out["strict"] = schema["strict"]
    return out


def _input_content(content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content or ""
    parts: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image = part.get("image_url")
            parts.append({"type": "input_image", "image_url": image.get("url") if isinstance(image, dict) else image})
        else:
            raise ValueError(f"The Responses API has no input form for a content part of type {kind!r}.")
    return parts


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text")


def _arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments if arguments is not None else {})
