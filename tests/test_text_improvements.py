"""Tests for interactive text-improvement helpers and HTTP route."""

from __future__ import annotations

from fastapi.testclient import TestClient

from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.server.text_improvements import (
    TextImprovementRequestData,
    build_text_improvement_prompt,
    parse_text_improvement_response,
    text_looks_sensitive,
)
from inqtrix.settings import AgentSettings, ModelSettings, ServerSettings, Settings


class _CapturingLLM:
    def __init__(self, response: str | None = None) -> None:
        self.prompt: str | None = None
        self.kwargs: dict[str, object] = {}
        self.response = response or (
            '{"improved_text":"Bitte analysiere den Bericht.",'
            '"change_summary":["Rechtschreibung korrigiert."],'
            '"warnings":[],"needs_clarification":false,'
            '"clarification_questions":[]}'
        )

    def complete(self, prompt: str, **kwargs: object) -> str:
        self.prompt = prompt
        self.kwargs = kwargs
        return self.response


    def is_available(self) -> bool:
        return True


class _DummySearch:
    def search(self, *args: object, **kwargs: object) -> GroundedSearchResult:
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


def _make_client(
    *,
    llm: _CapturingLLM | None = None,
    server_settings: ServerSettings | None = None,
) -> tuple[TestClient, _CapturingLLM]:
    active_llm = llm or _CapturingLLM()
    settings = Settings(
        models=ModelSettings(),
        agent=AgentSettings(),
        server=server_settings or ServerSettings(),
    )
    app = create_app(
        settings=settings,
        providers=ProviderContext(llm=active_llm, search=_DummySearch()),
    )
    return TestClient(app), active_llm


def test_chat_input_prompt_preserves_bilingual_contract() -> None:
    request = TextImprovementRequestData(
        context="chat_input",
        locale="de",
        text="Bitte korrigiere das hier.",
    )

    prompt = build_text_improvement_prompt(request)

    assert "AI research chat" in prompt
    assert "If the draft is German, improve it in German." in prompt
    assert "interface language: German" in prompt
    assert "Bitte korrigiere das hier." in prompt


def test_prompt_template_prompt_preserves_placeholders_contract() -> None:
    request = TextImprovementRequestData(
        context="prompt_template",
        guidance="Category: Function. Preserve the callable task shape.",
        locale="en",
        text="Use {topic} and @rules:style to write the answer.",
    )

    prompt = build_text_improvement_prompt(request)

    assert "prompt-engineering assistant" in prompt
    assert "Category: Function" in prompt
    assert "Preserve all placeholders" in prompt
    assert "interface language: English" in prompt
    assert "Use {topic} and @rules:style" in prompt


def test_parse_text_improvement_response_accepts_clean_json() -> None:
    result = parse_text_improvement_response(
        '{"improved_text":"Clean text.",'
        '"change_summary":["Cleaned."],'
        '"warnings":[],"needs_clarification":true,'
        '"clarification_questions":["Question?"]}'
    )

    assert result.improved_text == "Clean text."
    assert result.change_summary == ["Cleaned."]
    assert result.needs_clarification is True
    assert result.clarification_questions == ["Question?"]


def test_text_improvement_route_uses_chat_prompt() -> None:
    client, llm = _make_client()

    response = client.post(
        "/v1/text/improvements",
        json={
            "context": "chat_input",
            "locale": "de",
            "text": "Bitte analisiere den Bericht.",
        },
    )

    assert response.status_code == 200
    assert response.json()["improved_text"] == "Bitte analysiere den Bericht."
    assert llm.prompt is not None
    assert "AI research chat" in llm.prompt
    assert "Bitte analisiere den Bericht." in llm.prompt
    assert llm.kwargs["max_output_tokens"] == 2500


def test_text_improvement_route_uses_prompt_template_prompt() -> None:
    client, llm = _make_client()

    response = client.post(
        "/v1/text/improvements",
        json={
            "context": "prompt_template",
            "guidance": "Category: Context Pack. Preserve {{context}} exactly.",
            "locale": "en",
            "text": "Write about {{topic}}.",
        },
    )

    assert response.status_code == 200
    assert llm.prompt is not None
    assert "prompt-engineering assistant" in llm.prompt
    assert "Category: Context Pack" in llm.prompt
    assert "Write about {{topic}}." in llm.prompt


def test_text_improvement_route_rejects_invalid_context() -> None:
    client, _llm = _make_client()

    response = client.post(
        "/v1/text/improvements",
        json={"context": "unknown", "text": "Hello"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_text_improvement_route_rejects_sensitive_text_before_model_call() -> None:
    client, llm = _make_client()

    response = client.post(
        "/v1/text/improvements",
        json={
            "context": "chat_input",
            "text": "API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        },
    )

    assert response.status_code == 400
    assert "secret material" in response.json()["error"]["message"]
    assert llm.prompt is None
    assert text_looks_sensitive("password=abcdefghijklmnopqrstuvwxyz123456")


def test_text_improvement_route_surfaces_model_json_failures() -> None:
    client, _llm = _make_client(llm=_CapturingLLM(response="not json"))

    response = client.post(
        "/v1/text/improvements",
        json={"context": "chat_input", "text": "Hello"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"


def test_text_improvement_route_uses_bearer_auth_when_configured() -> None:
    client, _llm = _make_client(
        server_settings=ServerSettings(api_key="secret-token-123")
    )

    missing = client.post(
        "/v1/text/improvements",
        json={"context": "chat_input", "text": "Hello"},
    )
    allowed = client.post(
        "/v1/text/improvements",
        json={"context": "chat_input", "text": "Hello"},
        headers={"Authorization": "Bearer secret-token-123"},
    )

    assert missing.status_code == 401
    assert allowed.status_code == 200
