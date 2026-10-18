"""Reranker providers: cross-encoder re-scoring behind a Baukasten port.

API-first by project decision: the default implementation calls a
hosted rerank API (Cohere Rerank — natively, via Azure AI Foundry
serverless, or any endpoint speaking the Cohere rerank schema). Local
ONNX rerankers are a documented later option for air-gapped
deployments behind the same ABC; nothing here downloads or hosts
model weights.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import AgentCancelled
from inqtrix.providers.base import (
    MAX_PROVIDER_ATTEMPTS,
    ProviderRetryCallback,
    _RetryNoticeMixin,
    _check_provider_operation_deadline,
    _operation_deadline,
    _retry_delay_seconds,
    _sleep_before_retry,
)
from inqtrix.urls import sanitize_error

log = logging.getLogger("inqtrix")

_RETRYABLE_RERANK_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})


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


class CohereRerank(_RetryNoticeMixin, RerankerProvider):
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
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str,
        default_model: str,
        rerank_path: str = "/v2/rerank",
        timeout: float = REASONING_TIMEOUT,
    ) -> None:
        if not (api_key or "").strip():
            raise ValueError("CohereRerank requires a non-empty api_key")
        if not (base_url or "").strip():
            raise ValueError("CohereRerank requires a non-empty base_url")
        if not (default_model or "").strip():
            raise ValueError("CohereRerank requires a non-empty default_model")
        self._api_key = api_key
        clean_base = base_url.strip().rstrip("/")
        self._url = (
            clean_base
            if clean_base.endswith("/rerank")
            else clean_base + rerank_path
        )
        self._default_model = default_model
        self._timeout = timeout

    @property
    def default_model(self) -> str:
        """Model/deployment id used when a call names none."""
        return self._default_model

    def rerank(
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
        import time

        import httpx

        self._clear_retry_notices()
        active_model = (model or "").strip() or self._default_model
        request_body = {
            "model": active_model,
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        }
        operation_deadline = _operation_deadline(self._timeout, None)
        try:
            for attempt in range(1, MAX_PROVIDER_ATTEMPTS + 1):
                _check_provider_operation_deadline(
                    operation_deadline,
                    None,
                    label="Rerank call",
                )
                try:
                    response = httpx.post(
                        self._url,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        json=request_body,
                        timeout=max(0.001, operation_deadline - time.monotonic()),
                    )
                except httpx.TransportError as exc:
                    if attempt >= MAX_PROVIDER_ATTEMPTS:
                        raise
                    delay = _retry_delay_seconds(attempt - 1)
                    self._append_retry_notice({
                        "provider": "CohereRerank",
                        "model": active_model,
                        "operation": "rerank",
                        "error_code": type(exc).__name__,
                        "attempt": attempt,
                        "max_attempts": MAX_PROVIDER_ATTEMPTS,
                        "delay_seconds": round(delay, 3),
                        "configured_timeout_seconds": round(
                            float(self._timeout), 3
                        ),
                    })
                    log.warning(
                        "Rerank transport error (model=%s, attempt=%d/%d). "
                        "Retrying in %.1fs (error_type=%s).",
                        active_model,
                        attempt,
                        MAX_PROVIDER_ATTEMPTS,
                        delay,
                        type(exc).__name__,
                    )
                    _sleep_before_retry(delay, operation_deadline)
                    continue
                if (
                    response.status_code in _RETRYABLE_RERANK_STATUSES
                    and attempt < MAX_PROVIDER_ATTEMPTS
                ):
                    retry_after = response.headers.get("retry-after", "")
                    retry_after_seconds = (
                        float(retry_after)
                        if retry_after.replace(".", "", 1).isdigit()
                        else None
                    )
                    delay = _retry_delay_seconds(
                        attempt - 1,
                        retry_after_seconds,
                    )
                    self._append_retry_notice({
                        "provider": "CohereRerank",
                        "model": active_model,
                        "operation": "rerank",
                        "error_code": f"HTTP {response.status_code}",
                        "status_code": response.status_code,
                        "attempt": attempt,
                        "max_attempts": MAX_PROVIDER_ATTEMPTS,
                        "delay_seconds": round(delay, 3),
                        "configured_timeout_seconds": round(
                            float(self._timeout), 3
                        ),
                    })
                    log.warning(
                        "Rerank transient response (model=%s, status=%d, "
                        "attempt=%d/%d). Retrying in %.1fs.",
                        active_model,
                        response.status_code,
                        attempt,
                        MAX_PROVIDER_ATTEMPTS,
                        delay,
                    )
                    _sleep_before_retry(delay, operation_deadline)
                    continue
                response.raise_for_status()
                payload = response.json()
                break
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

    @contextmanager
    def observe_retries(
        self, callback: ProviderRetryCallback
    ) -> Iterator[object]:
        """Forward retry observation to the wrapped LLM when available.

        The listwise reranker performs no retries itself — the single
        completion call retries inside the injected LLM provider, so this
        adapter only exposes that provider's seam (mirrors
        ``ConfiguredLLMProvider``). Both reranker types thereby offer the
        same observation surface to the retrieval pipeline.
        """
        observer = getattr(self._llm, "observe_retries", None)
        if callable(observer):
            with observer(callback):
                yield self
        else:
            yield self

    def consume_retry_notices(self) -> list[dict[str, Any]]:
        """Forward retry diagnostics from the wrapped LLM when available."""
        consumer = getattr(self._llm, "consume_retry_notices", None)
        if not callable(consumer):
            return []
        notices = consumer()
        if not isinstance(notices, list):
            return []
        return [dict(item) for item in notices if isinstance(item, dict)]

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
