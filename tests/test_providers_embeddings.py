"""Offline pins for the OpenAI-SDK embedding provider contract.

The caller pairs vector ``i`` with chunk ``i`` positionally, so the
provider's index-sort, count guard, and whole-batch-or-nothing error
normalization carry the retrieval correctness of every indexed
document. None of this had test coverage before slice-based submission
landed on top of it.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from inqtrix.provider_failure_contract import (
    ProviderFailureKind,
    classify_provider_failure,
)
from inqtrix.providers.base import sdk_retry_after
from inqtrix.providers.embeddings import (
    EmbeddingProviderError,
    _OpenAISDKEmbeddings,
)


def _vector_for(text: str) -> list[float]:
    """Deterministic per-text vector so any misalignment is visible."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[0] / 255, digest[1] / 255, digest[2] / 255]


class _FakeEmbeddingsAPI:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[dict] = []

    def create(self, *, model: str, input: list[str]):  # noqa: A002 - SDK name
        self.calls.append({"model": model, "input": list(input)})
        return self._responder(model, list(input))


class _FakeClient:
    def __init__(self, responder) -> None:
        self.embeddings = _FakeEmbeddingsAPI(responder)


def _provider(responder) -> tuple[_OpenAISDKEmbeddings, _FakeClient]:
    client = _FakeClient(responder)
    provider = _OpenAISDKEmbeddings(
        client=client,
        default_model="test-embedding-model",
        selectable_models=None,
    )
    return provider, client


def _scrambled(model: str, texts: list[str]):
    """SDK-shaped response whose items arrive in reversed index order."""
    items = [
        SimpleNamespace(index=i, embedding=_vector_for(text))
        for i, text in enumerate(texts)
    ]
    return SimpleNamespace(data=list(reversed(items)))


# ---------------------------------------------------------------- #
# Ordering and count guarantees
# ---------------------------------------------------------------- #


def test_scrambled_sdk_response_is_reordered_by_index() -> None:
    """Vector i must belong to text i even when the SDK shuffles items."""
    provider, client = _provider(_scrambled)
    texts = [f"text-{i}" for i in range(7)]

    vectors = provider.embed_documents(texts)

    assert vectors == [_vector_for(text) for text in texts]
    assert len(client.embeddings.calls) == 1


def test_count_mismatch_fails_whole_batch_with_no_partial_result() -> None:
    def _drop_one(model: str, texts: list[str]):
        items = [
            SimpleNamespace(index=i, embedding=_vector_for(text))
            for i, text in enumerate(texts[:-1])
        ]
        return SimpleNamespace(data=items)

    provider, _ = _provider(_drop_one)

    with pytest.raises(EmbeddingProviderError, match="count mismatch"):
        provider.embed_documents(["a", "b", "c"])


def test_empty_input_short_circuits_without_any_request() -> None:
    provider, client = _provider(_scrambled)

    assert provider.embed_documents([]) == []
    assert client.embeddings.calls == []


def test_embed_query_is_exactly_one_request_for_one_text() -> None:
    provider, client = _provider(_scrambled)

    vector = provider.embed_query("query text")

    assert vector == _vector_for("query text")
    assert len(client.embeddings.calls) == 1
    assert client.embeddings.calls[0]["input"] == ["query text"]


def test_default_model_is_used_when_none_is_named() -> None:
    provider, client = _provider(_scrambled)

    provider.embed_documents(["a"])
    provider.embed_documents(["b"], model="explicit-model")

    assert client.embeddings.calls[0]["model"] == "test-embedding-model"
    assert client.embeddings.calls[1]["model"] == "explicit-model"


# ---------------------------------------------------------------- #
# Failure normalization keeps the transient cause classifiable
# ---------------------------------------------------------------- #


def test_duck_typed_429_stays_rate_limited_through_the_wrapper() -> None:
    """``raise … from`` must keep the SDK failure reachable for typing."""

    class _RateLimitError(Exception):
        status_code = 429

    def _raise(model: str, texts: list[str]):
        raise _RateLimitError("rate limit")

    provider, _ = _provider(_raise)

    with pytest.raises(EmbeddingProviderError) as excinfo:
        provider.embed_documents(["a"])

    classification = classify_provider_failure(excinfo.value)
    assert classification.kind is ProviderFailureKind.RATE_LIMITED
    assert classification.transient


def test_real_sdk_rate_limit_error_keeps_retry_after_readable() -> None:
    """The genuine ``openai.RateLimitError`` shape stays fully diagnosable."""
    from openai import RateLimitError

    response = MagicMock()
    response.status_code = 429
    response.headers = {"retry-after": "60"}
    error = RateLimitError(message="rate limit", response=response, body=None)

    def _raise(model: str, texts: list[str]):
        raise error

    provider, _ = _provider(_raise)

    with pytest.raises(EmbeddingProviderError) as excinfo:
        provider.embed_documents(["a"])

    classification = classify_provider_failure(excinfo.value)
    assert classification.kind is ProviderFailureKind.RATE_LIMITED
    assert sdk_retry_after(excinfo.value.__cause__) == "60"
