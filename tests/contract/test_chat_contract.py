"""Contract tests for ``/v1/chat/completions`` (non-streaming + SSE).

Locks the OpenAI-compatible wire format, the error envelopes, the
``mode`` semantics (including the mode/skip_search conflict rules that
must MOVE into the service layer, not vanish), and the payload caps.
"""

from __future__ import annotations

import json

import pytest

import inqtrix.research.web_research as web_research_module
from inqtrix.settings import AgentSettings, ServerSettings

from tests.contract._app import (
    make_contract_client,
    minimal_agent_result,
    parse_sse_frames,
)


# ------------------------------------------------------------------ #
# Non-streaming payload
# ------------------------------------------------------------------ #


def test_non_streaming_payload_shape(monkeypatch):
    resolution = {
        "node": "direct_chat",
        "model": "stub-model",
        "tier": "mid",
        "effort": "low",
        "model_source": "tier:mid",
        "effort_source": "tier:mid",
        "requested_tier": None,
    }

    def fake_run(*args, **kwargs):
        return minimal_agent_result(model_resolution=resolution)

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
        )

    assert response.status_code == 200
    payload = response.json()
    assert sorted(payload.keys()) == [
        "choices", "created", "id", "inqtrix", "model", "object", "usage",
    ]
    assert payload["id"].startswith("chatcmpl-")
    assert payload["object"] == "chat.completion"
    assert payload["model"] == "research-agent"
    assert payload["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Antwort mit Quelle [1]."},
            "finish_reason": "stop",
        }
    ]
    assert payload["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
    }
    assert payload["inqtrix"] == {"model_resolution": resolution}


def test_non_streaming_payload_omits_inqtrix_block_without_resolution(monkeypatch):
    def fake_run(*args, **kwargs):
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}], "stream": False},
        )

    assert response.status_code == 200
    assert "inqtrix" not in response.json()


# ------------------------------------------------------------------ #
# Streaming SSE sequence
# ------------------------------------------------------------------ #


def test_streaming_sse_chunk_sequence(monkeypatch):
    resolution = {"node": "direct_chat", "model": "stub-model"}

    def fake_run(question, **kwargs):
        progress_queue = kwargs.get("progress_queue")
        if progress_queue is not None:
            progress_queue.put(("progress", "Runde 1: Suche"))
        return minimal_agent_result(
            answer="Erste Antwort hier.",
            model_resolution=resolution,
        )

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": True,
            },
        ) as response:
            assert response.status_code == 200
            content_type = response.headers["content-type"]
            body = response.read().decode("utf-8")

    assert content_type.startswith("text/event-stream")
    frames = parse_sse_frames(body)
    # Chat chunks are bare `data:` frames without an `event:` line.
    assert all(name is None for name, _ in frames)

    datas = [data for _, data in frames]
    assert datas[-1] == "[DONE]"
    chunks = [json.loads(data) for data in datas[:-1]]

    # 1) Role announcement chunk comes first.
    first = chunks[0]
    assert first["object"] == "chat.completion.chunk"
    assert first["model"] == "research-agent"
    assert first["choices"] == [
        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
    ]
    chat_id = first["id"]
    assert chat_id.startswith("chatcmpl-")
    assert all(chunk["id"] == chat_id for chunk in chunks)

    contents = [
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
    ]
    # 2) Progress chunk uses the blockquote-code format.
    assert "> `Runde 1: Suche`\n>\n" in contents
    # 3) Separator between progress and answer.
    assert "\n\n---\n\n" in contents
    # 4) Model resolution rides an empty-delta chunk under "inqtrix".
    resolution_chunks = [chunk for chunk in chunks if "inqtrix" in chunk]
    assert resolution_chunks, "model_resolution chunk missing"
    assert resolution_chunks[0]["inqtrix"] == {"model_resolution": resolution}
    assert resolution_chunks[0]["choices"][0]["delta"] == {}
    # 5) The answer words arrive after the separator, in order.
    answer_text = "".join(
        content
        for content in contents[contents.index("\n\n---\n\n") + 1:]
        if content
    )
    assert answer_text == "Erste Antwort hier."
    # 6) Final chunk closes with finish_reason "stop" and an empty delta.
    last = chunks[-1]
    assert last["choices"] == [
        {"index": 0, "delta": {}, "finish_reason": "stop"}
    ]


def test_streaming_without_progress_skips_progress_and_separator(monkeypatch):
    def fake_run(question, **kwargs):
        assert kwargs.get("progress_queue") is None
        return minimal_agent_result(answer="Nur Antwort.")

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": True,
                "include_progress": False,
            },
        ) as response:
            body = response.read().decode("utf-8")

    frames = parse_sse_frames(body)
    chunks = [json.loads(data) for _, data in frames if data != "[DONE]"]
    contents = [
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
    ]
    assert "\n\n---\n\n" not in contents
    assert not any(content.startswith("> `") for content in contents)


# ------------------------------------------------------------------ #
# Error envelopes
# ------------------------------------------------------------------ #


def test_returned_terminal_failure_is_not_a_chat_completion(monkeypatch):
    safe_reason = "The required evidence block could not be verified safely."

    def fake_run(*args, **kwargs):
        result = minimal_agent_result(answer=safe_reason)
        result["result_state"]["_terminal_failure"] = {
            "type": "knowledge_grounding_format_invalid",
            "message": safe_reason,
        }
        return result

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "Hallo"}]},
        )

    assert response.status_code == 502
    assert response.json() == {
        "error": {
            "message": safe_reason,
            "type": "knowledge_grounding_format_invalid",
        }
    }


def test_invalid_json_body_envelope():
    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Invalid JSON body",
            "type": "invalid_request_error",
        }
    }


def test_missing_messages_envelope():
    with make_contract_client() as client:
        response = client.post("/v1/chat/completions", json={})

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Field 'question' or non-empty 'messages' is required",
            "type": "invalid_request_error",
        }
    }


@pytest.mark.parametrize("mode", ["knowledge", "reflect"])
def test_unknown_mode_envelope(mode: str):
    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "mode": mode,
            },
        )

    assert response.status_code == 400
    # The listing is registry-driven; removed modes fail loudly.
    assert response.json() == {
        "error": {
            "message": "mode must be 'research' or 'direct_llm'",
            "type": "invalid_request_error",
        }
    }


@pytest.mark.parametrize(
    ("mode", "skip_search", "message"),
    [
        (
            "direct_llm",
            False,
            "mode='direct_llm' conflicts with agent_overrides.skip_search=false",
        ),
        (
            "research",
            True,
            "mode='research' conflicts with agent_overrides.skip_search=true",
        ),
    ],
)
def test_mode_skip_search_conflicts_reject_with_400(mode, skip_search, message):
    """The conflict check must survive the move into the service layer."""
    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "mode": mode,
                "agent_overrides": {"skip_search": skip_search},
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {"message": message, "type": "invalid_request_error"}
    }


def test_message_count_cap_returns_413():
    with make_contract_client(
        server_settings=ServerSettings(max_message_count=2),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "user", "content": "eins"},
                    {"role": "assistant", "content": "zwei"},
                    {"role": "user", "content": "drei"},
                ],
            },
        )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "payload_too_large"


def test_input_token_cap_returns_413():
    with make_contract_client(
        server_settings=ServerSettings(max_total_input_tokens=10_000),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x" * 50_000}]},
        )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "payload_too_large"


def test_question_too_long_for_research_returns_400():
    with make_contract_client(
        agent_settings=AgentSettings(max_question_length=10),
    ) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "x" * 50}]},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


# ------------------------------------------------------------------ #
# Mode semantics observed by the agent entry point
# ------------------------------------------------------------------ #


@pytest.mark.parametrize(
    ("body_extra", "expected_skip_search"),
    [
        ({}, False),
        ({"mode": "research"}, False),
        ({"mode": "direct_llm"}, True),
        ({"agent_overrides": {"skip_search": True}}, True),
    ],
)
def test_mode_resolves_to_skip_search_setting(
    monkeypatch, body_extra, expected_skip_search
):
    seen: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        seen["skip_search"] = kwargs["settings"].skip_search
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", fake_run)

    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": False,
                **body_extra,
            },
        )

    assert response.status_code == 200
    assert seen["skip_search"] is expected_skip_search


# ------------------------------------------------------------------ #
# Incognito
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("stream", [False, True])
def test_incognito_reaches_the_chat_span_of_both_mouths(monkeypatch, stream):
    """The flag decides whether the turn's content may reach a trace; both
    the blocking and the streaming mouth hand it to the one chat span
    wrapper, and a request without it stays a normal chat."""
    import inqtrix.observability.otel as otel_module

    seen: list[object] = []
    original = otel_module.chat_thread_call

    def recording(fn, **kwargs):
        seen.append(kwargs.get("incognito"))
        return original(fn, **kwargs)

    monkeypatch.setattr(otel_module, "chat_thread_call", recording)
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **k: minimal_agent_result()
    )

    with make_contract_client() as client:
        for extra in ({"incognito": True}, {}):
            body = {
                "messages": [{"role": "user", "content": "Hallo"}],
                "stream": stream,
                **extra,
            }
            if stream:
                with client.stream("POST", "/v1/chat/completions", json=body) as response:
                    assert response.status_code == 200
                    response.read()
            else:
                response = client.post("/v1/chat/completions", json=body)
                assert response.status_code == 200

    assert seen == [True, False]


@pytest.mark.parametrize("value", ["true", 1, None, {}])
def test_an_incognito_flag_that_is_not_a_boolean_is_refused(monkeypatch, value):
    """A privacy request the server misread as absent would record what the
    caller asked to keep out; only true or false is accepted."""
    monkeypatch.setattr(
        web_research_module, "run_web_graph", lambda *a, **k: minimal_agent_result()
    )
    with make_contract_client() as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "Hallo"}],
                "incognito": value,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "Field 'incognito' must be true or false.",
            "type": "invalid_request_error",
        }
    }
