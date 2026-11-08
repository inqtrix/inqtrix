"""Bedrock API keys: which credential signs each call, and what the start says.

No live call (operator decision: Bedrock is proven offline only). A real
``bedrock-runtime`` client signs every request; a ``before-send`` handler reads
the signed ``Authorization`` header and answers the request, so nothing leaves
the process.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from botocore.awsrequest import AWSResponse

from inqtrix.providers import create_providers
from inqtrix.providers.bedrock import BedrockLLM
from inqtrix.settings import ModelSettings, ProviderSettings, Settings

KEY = "ABSKQmVkcm9ja0FQSUtleS1leGFtcGxl"
_BODY = json.dumps({
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
    "metrics": {"latencyMs": 1},
}).encode()


class _Body:
    def stream(self, **_: Any):
        yield _BODY


def _authorization(monkeypatch: pytest.MonkeyPatch, llm: BedrockLLM) -> str:
    """The ``Authorization`` header of one signed Converse call of *llm*."""
    seen: list[str] = []

    def answer(request: Any, **_: Any) -> AWSResponse:
        header = request.headers.get("Authorization", b"")
        seen.append(header.decode() if isinstance(header, bytes) else header)
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, _Body())

    # Every deadline rung gets a client of the same session, so each one is checked.
    built = llm._build_client

    def build(read_timeout: float) -> Any:
        client = built(read_timeout)
        client.meta.events.register("before-send.bedrock-runtime.Converse", answer)
        return client

    monkeypatch.setattr(llm, "_build_client", build)
    llm._clients_by_read_timeout.clear()
    assert llm.complete("Frage") == "ok"
    (header,) = seen
    return header


def test_an_api_key_signs_every_call_as_a_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = BedrockLLM(api_key=KEY, default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm) == f"Bearer {KEY}"
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_the_api_key_wins_over_the_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "env-key")
    llm = BedrockLLM(api_key=KEY, default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm) == f"Bearer {KEY}"


def test_without_an_api_key_boto3_takes_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "env-key")
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.bedrock"):
        llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm) == "Bearer env-key"
    assert "API key from AWS_BEARER_TOKEN_BEDROCK" in caplog.text


def test_without_any_key_calls_are_signed_with_aws_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.bedrock"):
        llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm).startswith("AWS4-HMAC-SHA256 ")
    assert "AWS credential chain (profile=-, region=eu-central-1)" in caplog.text


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_environment_key_signs_with_aws_credentials_and_says_so(
    blank: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """boto3 would send the blank value as a bearer key, which Bedrock refuses."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", blank)
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.bedrock"):
        llm = BedrockLLM(default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm).startswith("AWS4-HMAC-SHA256 ")
    assert "AWS_BEARER_TOKEN_BEDROCK is set but empty" in caplog.text


def test_a_blank_api_key_counts_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = BedrockLLM(api_key="  ", default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm).startswith("AWS4-HMAC-SHA256 ")


def test_an_api_key_and_a_profile_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="api_key and profile_name are mutually exclusive"):
        BedrockLLM(api_key=KEY, profile_name="team")


def test_the_start_line_names_the_api_key_and_never_its_value(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.bedrock"):
        BedrockLLM(api_key=KEY, default_model="eu.anthropic.claude-sonnet-4-6")

    assert "authenticate with the configured API key (region=eu-central-1)" in caplog.text
    assert KEY not in caplog.text


def _aws_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = tmp_path / "config"
    config.write_text("[profile team]\nregion = eu-central-1\n", encoding="utf-8")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))


def test_a_profile_next_to_the_environment_key_is_named_as_unused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _aws_config(tmp_path, monkeypatch)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "env-key")
    with caplog.at_level(logging.INFO, logger="inqtrix.providers.bedrock"):
        llm = BedrockLLM(profile_name="team", default_model="eu.anthropic.claude-sonnet-4-6")

    assert _authorization(monkeypatch, llm) == "Bearer env-key"
    assert "profile team is not used" in caplog.text


def _bedrock_settings(**providers: Any) -> Settings:
    return Settings(
        providers=ProviderSettings(llm_provider="bedrock", **providers),
        models=ModelSettings(reasoning_model="eu.anthropic.claude-sonnet-4-6"),
    )


def test_the_server_hands_the_key_to_the_provider_and_drops_the_profile(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="inqtrix.providers"):
        llm = create_providers(
            _bedrock_settings(aws_bearer_token_bedrock=KEY, aws_profile="team")
        ).llm

    assert "AWS_PROFILE=team is not used" in caplog.text
    assert KEY not in caplog.text
    bedrock = llm
    while not isinstance(bedrock, BedrockLLM):
        bedrock = bedrock._provider
    assert _authorization(monkeypatch, bedrock) == f"Bearer {KEY}"


def test_the_server_keeps_the_profile_next_to_a_blank_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _aws_config(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO, logger="inqtrix.providers"):
        create_providers(_bedrock_settings(aws_bearer_token_bedrock="  ", aws_profile="team"))

    assert "is not used" not in caplog.text
    assert "AWS credential chain (profile=team, region=eu-central-1)" in caplog.text


def test_a_key_in_the_dotenv_file_signs_the_servers_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """boto3 never sees the dotenv file; the setting carries the key to the provider."""
    (tmp_path / ".env").write_text(f"AWS_BEARER_TOKEN_BEDROCK={KEY}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    providers = ProviderSettings(_env_file=".env", llm_provider="bedrock")

    llm = create_providers(Settings(
        providers=providers, models=ModelSettings(reasoning_model="eu.anthropic.claude-sonnet-4-6")
    )).llm

    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    while not isinstance(llm, BedrockLLM):
        llm = llm._provider
    assert _authorization(monkeypatch, llm) == f"Bearer {KEY}"
