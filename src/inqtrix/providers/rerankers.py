"""Reranker providers: cross-encoder re-scoring behind a Baukasten port.

API-first by project decision: the default implementation calls a
hosted rerank API (Cohere Rerank — natively, via Azure AI Foundry
serverless, or any endpoint speaking the Cohere rerank schema). Local
ONNX rerankers are a documented later option for air-gapped
deployments behind the same ABC; nothing here downloads or hosts
model weights.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import AgentCancelled
from inqtrix.providers.attempts import call_on_loop, call_with_retries, estimate_request_tokens
from inqtrix.providers.base import (
    _operation_deadline,
)
from inqtrix.urls import sanitize_error

log = logging.getLogger(__name__)


class RerankerError(RuntimeError):
    """Raised when a rerank call fails (configured-but-broken is loud;
    an UNCONFIGURED reranker is represented by ``None``, never by a
    swallowed exception)."""


@dataclass(frozen=True)
class RerankResult:
    """One reranked document reference.

    Attributes:
        index: Position of the document in the request's input list.
        relevance_score: Provider-reported relevance (higher is more
            relevant; scale is provider-specific).
    """

    index: int
    relevance_score: float


class RerankerProvider(ABC):
    """Port for query/document relevance re-scoring.

    Implementations receive every credential via their constructor
    (never from the environment); only the settings bridge translates
    env configuration into constructor arguments.
    """

    @property
    @abstractmethod
    def default_model(self) -> str:
        """Model used when a call names none."""

    @abstractmethod
    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str | None = None,
    ) -> list[RerankResult]:
        """Score *documents* against *query*, best first.

        Args:
            query: The retrieval query.
            documents: Candidate texts in request order; results refer
                back via :attr:`RerankResult.index`.
            top_n: Number of results to return (provider-side cutoff).
            model: Optional model override.

        Raises:
            RerankerError: On any transport or provider failure —
                all-or-nothing, the caller decides about fallbacks
                visibly.
        """


class CohereRerank(RerankerProvider):
    """Cohere rerank API adapter (native endpoint or Azure AI Foundry).

    Speaks the Cohere ``/v2/rerank`` request schema with a Bearer key —
    the shape Azure AI Foundry serverless deployments of
    Cohere-rerank models expose as well.

    Args:
        api_key: Endpoint key. Required; Constructor-First.
        base_url: Endpoint base, e.g. ``https://api.cohere.com`` or an
            Azure serverless deployment URL
            (``https://<deployment>.<region>.models.ai.azure.com``).
        default_model: Model/deployment id used when a call names
            none, e.g. ``rerank-v3.5`` or the Azure deployment name.
        rerank_path: API path appended to *base_url*. ``/v2/rerank``
            is the current Cohere schema; override for endpoints that
            only expose ``/v1/rerank``. When *base_url* already ends
            in ``/rerank`` (operators paste the full endpoint URL from
            the provider portal — e.g. Azure AI Foundry's
            ``/providers/cohere/v2/rerank`` route), the URL is used
            verbatim and this path is ignored.
        timeout: Per-call timeout in seconds.
        default_headers: Optional headers sent with every request, for
            example the subscription header of an API gateway. The
            ``Authorization`` header with the key always wins.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        default_model: str,
        rerank_path: str = "/v2/rerank",
        timeout: float = REASONING_TIMEOUT,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        if not (api_key or "").strip():
            raise ValueError("CohereRerank requires a non-empty api_key")
        if not (base_url or "").strip():
            raise ValueError("CohereRerank requires a non-empty base_url")
        if not (default_model or "").strip():
            raise ValueError("CohereRerank requires a non-empty default_model")
        clean_base = base_url.strip().rstrip("/")
        self._url = (
            clean_base
            if clean_base.endswith("/rerank")
            else clean_base + rerank_path
        )
        self._default_model = default_model
        self._timeout = timeout
        self._headers = {
            **{
                name: value
                for name, value in (default_headers or {}).items()
                if name.lower() != "authorization"
            },
            "Authorization": f"Bearer {api_key}",
        }
        # Tests put a transport here; the client is built per loop on first use.
        self._client_kwargs: dict[str, Any] = {}
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, httpx.AsyncClient
        ] = weakref.WeakKeyDictionary()

    @property
    def default_model(self) -> str:
        """Model/deployment id used when a call names none."""
        return self._default_model

    def _async_client(self) -> httpx.AsyncClient:
        """The HTTP client of the running loop: one per adapter and loop, built on first use."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(**self._client_kwargs)
            self._clients[loop] = client
        return client

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str | None = None,
    ) -> list[RerankResult]:
        """Score *documents* via the rerank endpoint, best first; waits for :meth:`arerank`."""
        if not documents:
            return []
        return call_on_loop(self.arerank(query, documents, top_n=top_n, model=model))

    async def arerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str | None = None,
    ) -> list[RerankResult]:
        """Score *documents* via the rerank endpoint, best first."""
        if not documents:
            return []
        active_model = (model or "").strip() or self._default_model
        request_body = {
            "model": active_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        }
        deadline = _operation_deadline(self._timeout, None)
        client = self._async_client()

        async def send() -> Any:
            response = await client.post(
                self._url,
                headers=self._headers,
                json=request_body,
                timeout=max(0.001, deadline - time.monotonic()),
            )
            response.raise_for_status()
            return response.json()

        try:
            payload = await call_with_retries(
                send,
                provider_label="CohereRerank",
                model=active_model,
                operation="rerank",
                deadline=deadline,
                outer_deadline=None,
                timeout_label="Rerank call",
                configured_timeout_seconds=float(self._timeout),
                kind="rerank",
                estimated_input_tokens=estimate_request_tokens(request_body),
            )
        except AgentCancelled:
            # Run cancellation (a probe bound via provider_cancel_scope)
            # must unwind as-is, never be misclassified as a broken
            # rerank endpoint.
            raise
        except Exception as exc:  # noqa: BLE001 — normalized below, visibly
            log.warning(
                "Rerank call failed (model=%s, candidates=%d, error_type=%s)",
                active_model,
                len(documents),
                type(exc).__name__,
            )
            raise RerankerError(
                f"Rerank call failed for model {active_model!r}: "
                f"{sanitize_error(exc)}"
            ) from exc
        results = payload.get("results")
        if not isinstance(results, list):
            raise RerankerError(
                f"Rerank response missing 'results' (model {active_model!r})"
            )
        return [
            RerankResult(
                index=int(item["index"]),
                relevance_score=float(item["relevance_score"]),
            )
            for item in results
        ]

class LLMReranker(RerankerProvider):
    """Listwise reranking through the deployment's own LLM.

    The fallback for deployments without a rerank API contract: one
    LLM call orders the candidates ("RankGPT"-style listwise). Web
    research (2026) puts LLM listwise reranking at roughly 9x the cost
    and 35x the latency of a specialized cross-encoder, viable only on
    SMALL candidate lists — hence the hard ``max_candidates``
    truncation. This is a fallback, not a Cohere replacement.

    Args:
        llm: The deployment's LLM provider
            (``complete_with_metadata``). Injected by the composition
            root — this class never builds or configures providers.
        default_model: Resolved model id for the ``knowledge_rerank``
            routing node; empty lets the provider default apply.
        max_candidates: Hard cap on candidates sent to the LLM. Lists
            beyond it are truncated WITH a warning naming the dropped
            count — deeper pools are wasted on listwise LLM ranking,
            and the silent alternative would misreport recall.
        max_chars_per_document: Per-candidate text budget in the
            prompt; longer texts are cut (relevance judgement survives
            truncation far better than the context window survives
            twenty full chunks).
        timeout: Per-call timeout in seconds.
    """

    def __init__(
        self,
        llm: object,
        *,
        default_model: str = "",
        max_candidates: int = 20,
        max_chars_per_document: int = 800,
        timeout: float = REASONING_TIMEOUT,
    ) -> None:
        self._llm = llm
        self._default_model = default_model
        self._max_candidates = max_candidates
        self._max_chars_per_document = max_chars_per_document
        self._timeout = timeout

    @property
    def default_model(self) -> str:
        return self._default_model

    def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
        model: str | None = None,
    ) -> list[RerankResult]:
        from inqtrix.observability.context import bind_purpose
        from inqtrix.prompts import build_knowledge_rerank_prompt

        if len(documents) > self._max_candidates:
            log.warning(
                "LLM-Rerank: candidate list reduced from %d to %d; deeper pools are "
                "wasteful for listwise LLM ranking.",
                len(documents),
                self._max_candidates,
            )
        ranked_documents = documents[: self._max_candidates]
        prompt = build_knowledge_rerank_prompt(
            query,
            [
                document[: self._max_chars_per_document]
                for document in ranked_documents
            ],
        )
        active_model = (model or self._default_model or "").strip() or None
        try:
            with bind_purpose("knowledge_rerank"):
                response = self._llm.complete_with_metadata(
                    prompt, model=active_model, timeout=self._timeout
                )
        except AgentCancelled:
            # A cancelled run ends the call it waits for; that is the run's
            # end, never a broken rerank (as on the Cohere path).
            raise
        except Exception as exc:  # noqa: BLE001 — normalized below, visibly
            raise RerankerError(
                f"LLM rerank call failed: {sanitize_error(exc)}"
            ) from exc
        content = getattr(response, "content", "") or ""
        ranking = self._parse_ranking(content, len(ranked_documents))
        return [
            RerankResult(
                index=position,
                # Synthetic monotone score: listwise ranking yields an
                # order, not calibrated relevance (scale is
                # provider-specific per the RerankResult contract).
                relevance_score=1.0 - rank / len(ranking),
            )
            for rank, position in enumerate(ranking[:top_n])
        ]

    def _parse_ranking(self, content: str, document_count: int) -> list[int]:
        """Parse and validate the strict-JSON ranking (0-based out).

        Raises:
            RerankerError: On unparseable JSON, out-of-range or
                duplicate indices, or an incomplete ranking — a broken
                stage fails loudly, exactly like a broken rerank API.
        """
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match is None:
            raise RerankerError("LLM rerank response carries no JSON object")
        try:
            payload = json.loads(match.group(0))
            raw_ranking = payload["ranking"]
            ranking = [int(item) - 1 for item in raw_ranking]
        except (KeyError, TypeError, ValueError) as exc:
            raise RerankerError(
                f"LLM rerank response unparseable: {sanitize_error(exc)}"
            ) from exc
        if len(set(ranking)) != len(ranking):
            raise RerankerError("LLM rerank ranking contains duplicates")
        if any(index < 0 or index >= document_count for index in ranking):
            raise RerankerError(
                "LLM rerank ranking references out-of-range candidates"
            )
        if len(ranking) < document_count:
            raise RerankerError(
                f"LLM rerank ranking incomplete: {len(ranking)} of "
                f"{document_count} candidates ranked"
            )
        return ranking
