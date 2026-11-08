"""Extra HTTP headers from ``INQTRIX_LLM_EXTRA_HEADERS`` / ``INQTRIX_SEARCH_EXTRA_HEADERS``.

The values may be secrets (a gateway's subscription key), so every check
here also proves that no message and no log line carries one.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from inqtrix.providers import create_providers
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.providers.azure_web_search import AzureFoundryWebSearch
from inqtrix.providers.embeddings import AzureOpenAIEmbeddings, LiteLLMEmbeddings
from inqtrix.providers.headers import RESERVED_HEADERS, parse_extra_headers
from inqtrix.providers.litellm import LiteLLM
from inqtrix.providers.rerankers import CohereRerank
from inqtrix.server.container import build_knowledge_context
from inqtrix.settings import KnowledgeSettings, ModelSettings, ProviderSettings, Settings

SECRET = "gw-SECRET-7f3a"
SETTING = "INQTRIX_LLM_EXTRA_HEADERS"


def _refused(raw: str) -> str:
    with pytest.raises(ValueError) as refusal:
        parse_extra_headers(raw, setting=SETTING)
    message = str(refusal.value)
    assert SETTING in message
    assert SECRET not in message
    return message


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_setting_adds_no_header(raw: str) -> None:
    assert parse_extra_headers(raw, setting=SETTING) == {}


def test_the_headers_keep_the_operator_s_spelling() -> None:
    raw = json.dumps({"Ocp-Apim-Subscription-Key": SECRET, "x-tenant": "a b\tc"})

    assert parse_extra_headers(raw, setting=SETTING) == {
        "Ocp-Apim-Subscription-Key": SECRET,
        "x-tenant": "a b\tc",
    }


def test_text_that_is_not_json_is_refused() -> None:
    assert "not valid JSON" in _refused('{"X-Key": "' + SECRET + '",}')


@pytest.mark.parametrize("raw", [json.dumps([SECRET]), json.dumps(SECRET), "42", "null"])
def test_json_that_is_not_an_object_is_refused(raw: str) -> None:
    assert "must be a JSON object" in _refused(raw)


@pytest.mark.parametrize(
    "name", ["X Key" + SECRET, "X:" + SECRET, "Schlüssel" + SECRET, SECRET + "\n", ""]
)
def test_an_invalid_header_name_is_refused_without_repeating_it(name: str) -> None:
    message = _refused(json.dumps({name: "v"}))

    assert "not a valid HTTP header name" in message


@pytest.mark.parametrize("value", [1, None, True, [SECRET], {"k": SECRET}])
def test_a_value_that_is_not_text_is_refused(value: object) -> None:
    assert "X-Key" in _refused(json.dumps({"X-Key": value}))


@pytest.mark.parametrize(
    "value",
    [SECRET + "\r\nX-Injected: 1", SECRET + "\n", SECRET + "\x00", SECRET + "\x7f", SECRET + "ä"],
)
def test_a_value_outside_visible_ascii_is_refused(value: str) -> None:
    assert "X-Key" in _refused(json.dumps({"X-Key": value}))


@pytest.mark.parametrize("value", [" " + SECRET, SECRET + " ", "\t" + SECRET, SECRET + "\t"])
def test_a_value_with_a_space_or_tab_at_its_edge_is_refused_at_the_start(value: str) -> None:
    """HTTP clients reject such a value per request and quote it; the start refuses it by name."""
    assert "X-Key" in _refused(json.dumps({"X-Key": value}))


def test_two_names_that_differ_only_in_case_are_refused() -> None:
    assert "twice" in _refused(json.dumps({"X-Key": SECRET, "x-key": SECRET}))


def test_a_name_given_twice_is_refused() -> None:
    # JSON keeps the last of two equal names, so the first would vanish.
    raw = '{"X-Key": "' + SECRET + '", "X-Key": "' + SECRET + '-2"}'
    assert "names X-Key twice" in _refused(raw)


@pytest.mark.parametrize("name", sorted(RESERVED_HEADERS))
@pytest.mark.parametrize("spelling", [str.lower, str.upper, str.title])
def test_authentication_and_framing_headers_are_refused(name: str, spelling) -> None:
    written = spelling(name)

    assert f"sets {written}" in _refused(json.dumps({written: SECRET}))


# --- the factory ---------------------------------------------------------------


def _settings(**providers: object) -> Settings:
    return Settings(providers=ProviderSettings(**providers), models=ModelSettings(reasoning_model="R"))


_LLM = {
    "litellm": {},
    "anthropic": {"anthropic_api_key": "k"},
    "azure": {"azure_openai_endpoint": "https://t.openai.azure.com/", "azure_openai_api_key": "k"},
}
_SEARCH = {
    "perplexity": {},
    "azure_foundry": {
        "azure_ai_project_endpoint": "https://p.services.ai.azure.com/api/projects/p",
        "web_search_agent_name": "web-search",
        "azure_ai_project_api_key": "k",
    },
}


def _sent_headers(provider: object) -> dict[str, str]:
    """The extra headers of a built (traced) provider; Anthropic keeps its own."""
    try:
        return provider._default_headers
    except AttributeError:
        return provider._client_kwargs.get("default_headers", {})


@pytest.mark.parametrize("selector", sorted(_LLM))
def test_the_llm_headers_reach_the_selected_llm_and_no_search(
    selector: str, caplog: pytest.LogCaptureFixture
) -> None:
    raw = json.dumps({"X-Gateway-Key": SECRET})

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        ctx = create_providers(_settings(llm_provider=selector, llm_extra_headers=raw, **_LLM[selector]))

    assert _sent_headers(ctx.llm)["X-Gateway-Key"] == SECRET
    assert "X-Gateway-Key" not in _sent_headers(ctx.search)
    assert f"{SETTING} adds 1 header(s) to every {selector} call: X-Gateway-Key" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize("selector", sorted(_SEARCH))
def test_the_search_headers_reach_the_selected_search_and_not_the_llm(
    selector: str, caplog: pytest.LogCaptureFixture
) -> None:
    raw = json.dumps({"X-Gateway-Key": SECRET})

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        ctx = create_providers(
            _settings(search_provider=selector, search_extra_headers=raw, **_SEARCH[selector])
        )

    assert _sent_headers(ctx.search)["X-Gateway-Key"] == SECRET
    assert "X-Gateway-Key" not in _sent_headers(ctx.llm)
    assert "INQTRIX_SEARCH_EXTRA_HEADERS adds 1 header(s)" in caplog.text
    assert SECRET not in caplog.text


def test_an_invalid_setting_stops_the_start_without_its_value(caplog: pytest.LogCaptureFixture) -> None:
    raw = '{"X-Gateway-Key": "' + SECRET + '"'

    with pytest.raises(RuntimeError, match=SETTING) as stop:
        create_providers(_settings(llm_extra_headers=raw))

    assert SECRET not in str(stop.value)
    assert stop.value.__cause__ is None and stop.value.__suppress_context__
    assert SECRET not in caplog.text


def test_bedrock_refuses_the_headers_instead_of_dropping_them() -> None:
    with pytest.raises(RuntimeError, match="not supported with INQTRIX_LLM_PROVIDER=bedrock"):
        create_providers(
            _settings(llm_provider="bedrock", llm_extra_headers=json.dumps({"X-Gateway-Key": SECRET}))
        )


# --- on the wire ---------------------------------------------------------------


def test_the_openai_client_sends_the_headers_with_every_request() -> None:
    requests: list[httpx.Request] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    llm = LiteLLM(
        api_key="test-key",
        base_url="http://gateway.test/v1",
        default_model="m",
        default_headers={"X-Gateway-Key": SECRET},
    )
    llm._client_kwargs["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(gateway))

    assert llm.complete("eins") == "ok"
    assert llm.complete("zwei") == "ok"

    assert [request.headers["x-gateway-key"] for request in requests] == [SECRET, SECRET]
    assert requests[0].headers["authorization"] == "Bearer test-key"


def test_the_anthropic_request_carries_the_headers_and_keeps_its_key() -> None:
    sent: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1}},
        )

    llm = AnthropicLLM(
        api_key="real-key",
        default_headers={"X-Gateway-Key": SECRET, "X-API-KEY": "other", "user-agent": "gateway-agent"},
    )
    llm._client_kwargs["transport"] = httpx.MockTransport(gateway)

    assert llm.complete("Frage") == "ok"

    (request,) = sent
    assert request.headers["x-gateway-key"] == SECRET
    assert request.headers.get_list("x-api-key") == ["real-key"]
    assert request.headers.get_list("user-agent") == ["gateway-agent"]
    assert request.headers.get_list("content-type") == ["application/json"]


@pytest.mark.parametrize("spelling", ["api-key", "API-Key"])
def test_the_foundry_project_key_wins_over_any_api_key_header(spelling: str) -> None:
    search = AzureFoundryWebSearch(
        project_endpoint="https://p.services.ai.azure.com/api/projects/p",
        agent_name="web-search",
        api_key="project-key",
        default_headers={spelling: "other", "X-Gateway-Key": SECRET},
    )

    assert search._client_kwargs["default_headers"] == {"X-Gateway-Key": SECRET, "api-key": "project-key"}


# --- embeddings -----------------------------------------------------------------

_EMBEDDING = {
    "object": "list",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "model": "m",
    "usage": {"prompt_tokens": 1, "total_tokens": 1},
}


def _embedding_requests(embeddings: object) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_EMBEDDING)

    embeddings._client = embeddings._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(gateway))
    )
    assert embeddings.embed_query("Frage") == [0.1, 0.2]
    return requests


def test_both_embedding_clients_send_the_headers() -> None:
    litellm = LiteLLMEmbeddings(
        api_key="key", base_url="http://gateway.test/v1", default_headers={"X-Gateway-Key": SECRET}
    )
    azure = AzureOpenAIEmbeddings(
        api_key="azure-key",
        azure_endpoint="https://resource.openai.azure.com",
        default_headers={"X-Gateway-Key": SECRET},
    )

    (sent,) = _embedding_requests(litellm)
    assert sent.headers["x-gateway-key"] == SECRET
    (sent,) = _embedding_requests(azure)
    assert sent.headers["x-gateway-key"] == SECRET
    assert sent.headers["api-key"] == "azure-key"


@pytest.mark.parametrize("selector", ["openai_compatible", "azure"])
def test_the_embedding_headers_reach_the_built_embeddings(
    selector: str, caplog: pytest.LogCaptureFixture
) -> None:
    knowledge = KnowledgeSettings(
        enabled=True,
        embedding_provider=selector,
        embedding_extra_headers=json.dumps({"X-Gateway-Key": SECRET}),
        embedding_azure_endpoint="https://resource.openai.azure.com",
        embedding_azure_api_key="azure-key",
    )

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        context = build_knowledge_context(Settings(knowledge=knowledge))

    assert context.embeddings._provider._client.default_headers["X-Gateway-Key"] == SECRET
    assert (
        "INQTRIX_EMBEDDING_EXTRA_HEADERS adds 1 header(s) to every "
        f"{selector} embedding call: X-Gateway-Key"
    ) in caplog.text
    assert SECRET not in caplog.text


def test_an_invalid_embedding_setting_stops_the_start_without_its_value() -> None:
    knowledge = KnowledgeSettings(
        enabled=True, embedding_extra_headers=json.dumps({"Authorization": SECRET})
    )

    with pytest.raises(RuntimeError, match="INQTRIX_EMBEDDING_EXTRA_HEADERS sets Authorization") as stop:
        build_knowledge_context(Settings(knowledge=knowledge))

    assert SECRET not in str(stop.value)


# --- rerank ----------------------------------------------------------------------


def test_the_cohere_reranker_sends_the_headers_and_keeps_its_key() -> None:
    requests: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.5}]})

    reranker = CohereRerank(
        api_key="rerank-key",
        base_url="https://rerank.example",
        default_model="rerank-v3.5",
        default_headers={"X-Gateway-Key": SECRET, "authorization": "other"},
    )
    reranker._client_kwargs["transport"] = httpx.MockTransport(gateway)

    assert [result.index for result in reranker.rerank("Frage", ["eins"], top_n=1)] == [0]
    (sent,) = requests
    assert sent.headers["x-gateway-key"] == SECRET
    assert sent.headers.get_list("authorization") == ["Bearer rerank-key"]


def _cohere_knowledge(**overrides: object) -> KnowledgeSettings:
    return KnowledgeSettings(
        enabled=True,
        reranker_provider="cohere",
        reranker_base_url="https://rerank.example",
        reranker_api_key="rerank-key",
        reranker_model="rerank-v3.5",
        **overrides,
    )


def test_the_rerank_headers_reach_the_built_reranker(caplog: pytest.LogCaptureFixture) -> None:
    knowledge = _cohere_knowledge(reranker_extra_headers=json.dumps({"X-Gateway-Key": SECRET}))

    with caplog.at_level(logging.INFO, logger="inqtrix"):
        context = build_knowledge_context(Settings(knowledge=knowledge))

    reranker = getattr(context.reranker, "_provider", context.reranker)
    assert reranker._headers["X-Gateway-Key"] == SECRET
    assert (
        "INQTRIX_RERANKER_EXTRA_HEADERS adds 1 header(s) to every cohere rerank call: X-Gateway-Key"
        in caplog.text
    )
    assert SECRET not in caplog.text


def test_an_invalid_rerank_setting_stops_the_start_without_its_value() -> None:
    knowledge = _cohere_knowledge(reranker_extra_headers='{"X-Gateway-Key": "' + SECRET + '",}')

    with pytest.raises(RuntimeError, match="INQTRIX_RERANKER_EXTRA_HEADERS is not valid JSON") as stop:
        build_knowledge_context(Settings(knowledge=knowledge))

    assert SECRET not in str(stop.value)


def test_the_llm_reranker_says_that_it_ignores_the_rerank_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    knowledge = KnowledgeSettings(
        enabled=True,
        reranker_provider="llm",
        reranker_extra_headers=json.dumps({"X-Gateway-Key": SECRET}),
    )
    llm = create_providers(_settings()).llm

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        build_knowledge_context(Settings(knowledge=knowledge), llm=llm)

    assert "INQTRIX_RERANKER_EXTRA_HEADERS is set but the llm reranker" in caplog.text
    assert SECRET not in caplog.text


BINDING_BETA = "thinking-binding-controls-2026-08-01"


@pytest.mark.parametrize(
    ("operator_beta", "sent"),
    [
        (None, BINDING_BETA),
        ("files-api-2025-04-14", f"files-api-2025-04-14,{BINDING_BETA}"),
        (BINDING_BETA, BINDING_BETA),
    ],
)
def test_an_operator_beta_joins_the_binding_beta(operator_beta: str | None, sent: str) -> None:
    import asyncio

    requests: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"content": [], "usage": {"input_tokens": 1, "output_tokens": 1}})

    llm = AnthropicLLM(
        api_key="real-key",
        default_headers={"anthropic-beta": operator_beta} if operator_beta else None,
    )
    llm._client_kwargs["transport"] = httpx.MockTransport(gateway)

    asyncio.run(llm._arequest_json(
        payload={"model": "claude-opus-5-5", "max_tokens": 16, "messages": []},
        timeout=10.0,
        beta=BINDING_BETA,
    ))

    (request,) = requests
    assert request.headers.get_list("anthropic-beta") == [sent]


def test_rerank_headers_without_a_reranker_say_that_they_are_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    knowledge = KnowledgeSettings(
        enabled=True,
        reranker_provider="none",
        reranker_extra_headers=json.dumps({"X-Gateway-Key": SECRET}),
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        build_knowledge_context(Settings(knowledge=knowledge))

    assert "INQTRIX_RERANKER_EXTRA_HEADERS is set but no reranker is configured" in caplog.text
    assert SECRET not in caplog.text


def test_knowledge_headers_with_knowledge_off_say_that_they_are_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    knowledge = KnowledgeSettings(
        enabled=False,
        embedding_extra_headers=json.dumps({"X-Gateway-Key": SECRET}),
        reranker_extra_headers=json.dumps({"X-Gateway-Key": SECRET}),
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        assert build_knowledge_context(Settings(knowledge=knowledge)) is None

    for setting in ("INQTRIX_EMBEDDING_EXTRA_HEADERS", "INQTRIX_RERANKER_EXTRA_HEADERS"):
        assert f"{setting} is set but the knowledge engine is off" in caplog.text
    assert SECRET not in caplog.text
