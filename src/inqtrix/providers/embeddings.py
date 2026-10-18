"""Embedding providers: the dense-vector sibling of LLMProvider.

``LLMProvider`` stays clean (completion/structured output only);
embedding is a separate capability with its own ABC, mirrored
constructor discipline (Constructor-First — no env reads), and its own
selectable-model surface feeding the embedding catalog.

First implementation: :class:`LiteLLMEmbeddings` against any
OpenAI-compatible ``/embeddings`` endpoint (LiteLLM proxy, OpenAI,
vLLM, Ollama) — zero new dependencies, reusing the ``openai`` SDK
already in the tree. A local in-process provider is a staged
alternative behind the same ABC for deployments without an embeddings
endpoint.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.urls import sanitize_error

log = logging.getLogger("inqtrix")


class EmbeddingProviderError(RuntimeError):
    """Raised when an embedding call fails after provider-side handling.

    Always carries a sanitized message (no credentials, no raw provider
    bodies); callers surface it as a visible HTTP error or failed job,
    never as a silent empty-vector fallback.
    """


class EmbeddingProvider(ABC):
    """Dense embedding capability for the knowledge engine.

    Implementations embed both documents (at ingestion) and queries
    (at retrieval) with an explicit model id — the collection's
    immutable ``embedding_model`` — and report which models the UI may
    offer at collection creation.
    """

    @property
    def selectable_embedding_models(self) -> list[str]:
        """Model ids the UI may offer in the collection-creation picker.

        Empty (the default) means the deployment offers only the
        configured default model and the picker stays hidden.
        """
        return []

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Model used when a collection names none explicitly."""

    @abstractmethod
    def embed_documents(
        self, texts: list[str], *, model: str | None = None
    ) -> list[list[float]]:
        """Embed document chunks in input order.

        Args:
            texts: Non-empty chunk texts.
            model: Embedding model id; ``None`` uses ``default_model``.

        Returns:
            One vector per input text, in order.

        Raises:
            EmbeddingProviderError: On transport or API failure. Never
                returns partial results — a failed batch fails whole so
                the caller's chunk/embedding pairing cannot skew.
        """

    @abstractmethod
    def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        """Embed one retrieval query with the same model as the corpus."""


class _OpenAISDKEmbeddings(EmbeddingProvider):
    """Shared embed flow for providers speaking the OpenAI SDK.

    Subclasses construct the SDK client (plain OpenAI-compatible vs
    Azure deployment-based auth); batching, ordering, count checks,
    and the loud error normalization are defined exactly once here.
    """

    def __init__(
        self,
        *,
        client,
        default_model: str,
        selectable_models: list[str] | None,
    ) -> None:
        if not (default_model or "").strip():
            raise ValueError(
                f"{type(self).__name__} requires a non-empty default_model"
            )
        self._client = client
        self._default_model = default_model
        self._selectable_models = list(selectable_models or [])

    @property
    def selectable_embedding_models(self) -> list[str]:
        """Model ids offered in the collection-creation picker."""
        return list(self._selectable_models)

    @property
    def default_model(self) -> str:
        """Model used when a collection names none explicitly."""
        return self._default_model

    def _embed(self, texts: list[str], model: str | None) -> list[list[float]]:
        active_model = (model or "").strip() or self._default_model
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=active_model,
                input=texts,
            )
        except Exception as exc:  # noqa: BLE001 — normalized below, visibly
            log.warning(
                "Embedding call failed (model=%s, batch=%d, error_type=%s)",
                active_model,
                len(texts),
                type(exc).__name__,
            )
            raise EmbeddingProviderError(
                f"Embedding call failed for model {active_model!r}: "
                f"{sanitize_error(exc)}"
            ) from exc
        ordered = sorted(response.data, key=lambda item: item.index)
        if len(ordered) != len(texts):
            raise EmbeddingProviderError(
                f"Embedding response count mismatch: sent {len(texts)} "
                f"inputs, received {len(ordered)} vectors (model "
                f"{active_model!r})"
            )
        return [list(item.embedding) for item in ordered]

    def embed_documents(
        self, texts: list[str], *, model: str | None = None
    ) -> list[list[float]]:
        """Embed document chunks in input order (whole batch or nothing)."""
        return self._embed(texts, model)

    def embed_query(self, text: str, *, model: str | None = None) -> list[float]:
        """Embed one retrieval query."""
        vectors = self._embed([text], model)
        return vectors[0]


class LiteLLMEmbeddings(_OpenAISDKEmbeddings):
    """Embeddings via an OpenAI-compatible ``/embeddings`` endpoint.

    Args:
        api_key: API key for the endpoint. Required; Constructor-First
            (the settings bridge translates env configuration into
            this argument, the provider never reads the environment).
        base_url: Endpoint base URL including the ``/v1`` suffix.
        default_model: Embedding model used when a call names none.
        selectable_models: Model ids offered in the UI picker; empty
            keeps the picker hidden.
        timeout: Per-call timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "http://localhost:4000/v1",
        default_model: str = "text-embedding-3-small",
        selectable_models: list[str] | None = None,
        timeout: float = REASONING_TIMEOUT,
    ) -> None:
        from openai import OpenAI

        if not (api_key or "").strip():
            raise ValueError("LiteLLMEmbeddings requires a non-empty api_key")
        self._timeout = float(timeout)
        super().__init__(
            client=OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=timeout,
                max_retries=0,
            ),
            default_model=default_model,
            selectable_models=selectable_models,
        )


class AzureOpenAIEmbeddings(_OpenAISDKEmbeddings):
    """Embeddings via Azure OpenAI deployment-based authentication.

    For Azure resources without the OpenAI-compatible ``/openai/v1``
    surface: requests go to
    ``{endpoint}/openai/deployments/{model}/embeddings`` with the
    ``api-key`` header and an explicit ``api-version``. The model id
    used in calls is the Azure DEPLOYMENT name.

    Args:
        api_key: Azure OpenAI / AI-Foundry key. Required;
            Constructor-First — only the settings bridge reads env.
        azure_endpoint: Resource endpoint. An AI-Foundry PROJECT
            endpoint (``.../api/projects/<name>``) is accepted and
            reduced to the resource root automatically — deployments
            are served there, not under the project path.
        api_version: Azure OpenAI data-plane API version. The default
            is the 2024-10-21 GA version; override only when a newer
            deployment type demands it.
        default_model: Deployment name used when a call names none.
        selectable_models: Deployment names offered in the UI picker.
        timeout: Per-call timeout in seconds.
    """

    def __init__(
        self,
        api_key: str,
        *,
        azure_endpoint: str,
        api_version: str = "2024-10-21",
        default_model: str = "text-embedding-3-large",
        selectable_models: list[str] | None = None,
        timeout: float = REASONING_TIMEOUT,
    ) -> None:
        from openai import AzureOpenAI

        if not (api_key or "").strip():
            raise ValueError(
                "AzureOpenAIEmbeddings requires a non-empty api_key"
            )
        if not (azure_endpoint or "").strip():
            raise ValueError(
                "AzureOpenAIEmbeddings requires a non-empty azure_endpoint"
            )
        resource_root = re.sub(
            r"/api/projects/.*$", "", azure_endpoint.strip().rstrip("/")
        )
        self._timeout = float(timeout)
        super().__init__(
            client=AzureOpenAI(
                azure_endpoint=resource_root,
                api_key=api_key,
                api_version=api_version,
                timeout=timeout,
                max_retries=0,
            ),
            default_model=default_model,
            selectable_models=selectable_models,
        )
