"""Native tool-calling contract of the provider layer.

Secures: the shared OpenAI-response -> ChatTurn mapping (id passthrough
and synthesis, strict argument parsing), the LiteLLM/Azure ``chat()``
request shape (tools forwarded, message array verbatim), and the loud
base-class default for providers that never opted in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inqtrix.exceptions import AgentStructuredOutputError
from inqtrix.providers.base import (
    ChatTurn,
    LLMProvider,
    chat_turn_from_openai_response,
)
from inqtrix.providers.litellm import LiteLLM


def _tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _response(
    *,
    content: str | None,
    tool_calls: list | None = None,
    finish_reason: str = "stop",
):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content, tool_calls=tool_calls
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def test_chat_turn_maps_text_tool_calls_and_usage():
    response = _response(
        content=None,
        tool_calls=[
            _tool_call("call_abc", "web_instant", '{"query": "EU AI Act"}')
        ],
        finish_reason="tool_calls",
    )
    turn = chat_turn_from_openai_response(response, model="gpt-test")
    assert turn.text == ""
    assert turn.finish_reason == "tool_calls"
    assert turn.prompt_tokens == 11
    assert turn.completion_tokens == 7
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call.id, call.name) == ("call_abc", "web_instant")
    assert call.arguments == {"query": "EU AI Act"}


def test_chat_turn_synthesizes_missing_tool_call_id():
    response = _response(
        content="",
        tool_calls=[_tool_call("", "ask_user", "{}")],
        finish_reason="tool_calls",
    )
    turn = chat_turn_from_openai_response(response, model="gpt-test")
    assert turn.tool_calls[0].id.startswith("call_")
    assert len(turn.tool_calls[0].id) > len("call_")


def test_chat_turn_rejects_invalid_arguments_loudly():
    broken = _response(
        content=None,
        tool_calls=[_tool_call("call_1", "web_instant", "{not json")],
    )
    with pytest.raises(AgentStructuredOutputError):
        chat_turn_from_openai_response(broken, model="gpt-test")
    non_object = _response(
        content=None,
        tool_calls=[_tool_call("call_1", "web_instant", '["a", "b"]')],
    )
    with pytest.raises(AgentStructuredOutputError):
        chat_turn_from_openai_response(non_object, model="gpt-test")


def test_chat_turn_empty_arguments_default_to_empty_object():
    response = _response(
        content=None,
        tool_calls=[_tool_call("call_1", "ask_user", "")],
    )
    turn = chat_turn_from_openai_response(response, model="gpt-test")
    assert turn.tool_calls[0].arguments == {}


def test_litellm_chat_forwards_messages_and_tools():
    client = MagicMock()
    client.chat.completions.create.return_value = _response(
        content="Fertig.", tool_calls=None
    )
    provider = LiteLLM(api_key="test-key", default_model="gpt-4o")
    provider._client = client

    messages = [
        {"role": "system", "content": "Du bist der Kernel."},
        {"role": "user", "content": "Frage."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "web_instant", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Ergebnis."},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "web_instant",
                "description": "Websuche",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    turn = provider.chat(messages, tools=tools, max_output_tokens=99)

    assert isinstance(turn, ChatTurn)
    assert turn.text == "Fertig."
    call_kwargs = client.chat.completions.create.call_args.kwargs
    # The conversation is sent VERBATIM — the agent loop owns history.
    assert call_kwargs["messages"] == messages
    assert call_kwargs["tools"] == tools
    assert call_kwargs["max_tokens"] == 99
    assert call_kwargs["stream"] is False


def test_litellm_chat_without_tools_omits_the_parameter():
    client = MagicMock()
    client.chat.completions.create.return_value = _response(content="Hi.")
    provider = LiteLLM(api_key="test-key", default_model="gpt-4o")
    provider._client = client

    provider.chat([{"role": "user", "content": "Hi"}])
    assert "tools" not in client.chat.completions.create.call_args.kwargs


def test_configured_provider_forwards_tool_calling():
    """The wrapper must not swallow a custom provider's tool capability.

    Same bug class its ``llm_capabilities`` forward guards against: a
    wrapped tool-capable provider inheriting the base defaults would
    read as tool-incapable and fail the kernel registration gate.
    """
    from inqtrix.providers.base import ConfiguredLLMProvider
    from inqtrix.settings import ModelSettings

    class ToolCapable(LLMProvider):
        def __init__(self):
            self.chat_calls = []

        def complete(self, prompt, **kwargs):  # type: ignore[override]
            return "ok"

        def is_available(self) -> bool:
            return True

        def supports_tool_calls(self, *, model=None) -> bool:
            return True

        def chat(self, messages, *, tools=None, model=None, **kwargs):  # type: ignore[override]
            self.chat_calls.append({"model": model, "tools": tools})
            return ChatTurn(
                text="Hi.",
                tool_calls=(),
                finish_reason="stop",
                model=str(model),
                prompt_tokens=1,
                completion_tokens=1,
                raw=None,
            )

    inner = ToolCapable()
    wrapped = ConfiguredLLMProvider(
        inner, ModelSettings(reasoning_model="wrapped-default")
    )
    assert wrapped.supports_tool_calls() is True
    turn = wrapped.chat([{"role": "user", "content": "Hi"}])
    assert turn.text == "Hi."
    # The wrapper defaults the model from its settings, like complete().
    assert inner.chat_calls[0]["model"] == "wrapped-default"

    class Plain(LLMProvider):
        def complete(self, prompt, **kwargs):  # type: ignore[override]
            return "ok"

        def is_available(self) -> bool:
            return True

    plain_wrapped = ConfiguredLLMProvider(
        Plain(), ModelSettings(reasoning_model="m")
    )
    assert plain_wrapped.supports_tool_calls() is False
    with pytest.raises(NotImplementedError):
        plain_wrapped.chat([{"role": "user", "content": "Hi"}])


def test_provider_opt_in_defaults_are_loud():
    class MinimalProvider(LLMProvider):
        def complete(self, prompt, **kwargs):  # type: ignore[override]
            return "ok"

        def is_available(self) -> bool:
            return True

    provider = MinimalProvider()
    assert provider.supports_tool_calls() is False
    with pytest.raises(NotImplementedError):
        provider.chat([{"role": "user", "content": "Hi"}])
    assert LiteLLM(
        api_key="k", default_model="m"
    ).supports_tool_calls() is True


def test_capability_questions_survive_every_decorator():
    """A provider that declares no capability answers ``False`` at each layer.

    A provider reaches a caller through up to three decorators
    (configuration, tracing, execution authority). Every one of them
    forwards the capability question, so a single naive forward turns
    "this adapter never declared tool calling" into an AttributeError
    raised at whoever asked — for the kernel registration gate that
    means a dead server instead of a missed gate.
    """
    from inqtrix.execution_authority import AuthorizationGuardedLLM
    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.observability.provider_tracing import TracingLLMProvider
    from inqtrix.providers.base import (
        ConfiguredLLMProvider,
        reported_capability,
    )
    from inqtrix.settings import ModelSettings

    class UndeclaredLLM:
        """Adapter that implements the prompt surface and nothing else."""

        def complete(self, prompt, **kwargs):
            return "ok"

        def is_available(self) -> bool:
            return True

    inner = UndeclaredLLM()
    assert reported_capability(inner, "supports_tool_calls") is False
    assert reported_capability(inner, "supports_structured_output") is False

    layered = [
        ConfiguredLLMProvider(inner, ModelSettings(reasoning_model="m")),
        TracingLLMProvider(
            inner,
            provider_name="undeclared",
            policy=ContentCapturePolicy(
                configured_capture=False, max_attr_bytes=256
            ),
        ),
        AuthorizationGuardedLLM(inner, lambda: None),
    ]
    for decorated in layered:
        assert decorated.supports_tool_calls() is False
        assert decorated.supports_structured_output() is False


_WEB_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "web_instant",
            "description": "Websuche",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


def _azure_with_client(client):
    from inqtrix.providers.azure import AzureOpenAILLM

    provider = AzureOpenAILLM(
        azure_endpoint="https://test-resource.openai.azure.com/",
        api_key="test-key",
        default_model="my-deployment",
    )
    provider._client = client
    return provider


@pytest.mark.parametrize("build", ["litellm", "azure"])
def test_chat_sends_the_obligation_to_call_a_tool_only_when_asked(build):
    """``tool_choice`` reaches the request when the caller binds it, and a
    plain turn sends none (the model stays free to answer in text)."""
    client = MagicMock()
    client.chat.completions.create.return_value = _response(content="Hi.")
    provider = (
        LiteLLM(api_key="test-key", default_model="gpt-4o")
        if build == "litellm"
        else _azure_with_client(client)
    )
    provider._client = client
    messages = [{"role": "user", "content": "Prüfe."}]

    provider.chat(messages, tools=_WEB_TOOL, tool_choice="required")
    assert client.chat.completions.create.call_args.kwargs["tool_choice"] == "required"

    provider.chat(messages, tools=_WEB_TOOL)
    assert "tool_choice" not in client.chat.completions.create.call_args.kwargs


def test_the_obligation_to_call_a_tool_survives_every_decorator():
    """A provider reaches the kernel through up to four decorators. A layer
    with a fixed signature that drops ``tool_choice`` lets a role end with
    prose instead of its typed result, so each one forwards it — and none
    invents it for a plain turn."""
    from inqtrix.execution_authority import AuthorizationGuardedLLM
    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.observability.provider_tracing import TracingLLMProvider
    from inqtrix.providers.base import (
        ConfiguredLLMProvider,
        UntrustedContentRuleProvider,
    )
    from inqtrix.settings import ModelSettings

    class Recording(LLMProvider):
        def __init__(self):
            self.kwargs: list[dict] = []

        def complete(self, prompt, **kwargs):  # type: ignore[override]
            return "ok"

        def is_available(self) -> bool:
            return True

        def supports_tool_calls(self, *, model=None) -> bool:
            return True

        def chat(self, messages, **kwargs):  # type: ignore[override]
            self.kwargs.append(kwargs)
            return ChatTurn(
                text="Hi.",
                tool_calls=(),
                finish_reason="stop",
                model="m",
                prompt_tokens=1,
                completion_tokens=1,
                raw=None,
            )

    inner = Recording()
    served = AuthorizationGuardedLLM(
        TracingLLMProvider(
            UntrustedContentRuleProvider(
                ConfiguredLLMProvider(inner, ModelSettings(reasoning_model="m"))
            ),
            provider_name="recording",
            policy=ContentCapturePolicy(configured_capture=False, max_attr_bytes=256),
        ),
        lambda: None,
    )

    served.chat([{"role": "user", "content": "Prüfe."}], tools=_WEB_TOOL, tool_choice="required")
    served.chat([{"role": "user", "content": "Hi"}], tools=_WEB_TOOL)

    assert inner.kwargs[0]["tool_choice"] == "required"
    assert "tool_choice" not in inner.kwargs[1]
