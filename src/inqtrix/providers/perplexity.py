"""Perplexity Agent API search provider.

Web search via Perplexity's Agent API through the native ``perplexityai``
SDK (``client.responses.create``). Each query returns a synthesized answer
with inline ``[id]`` citations plus a per-source ``search_results`` list
(id, url, title, full snippet, date) -- both surfaced through
:class:`~inqtrix.search_result.GroundedSearchResult`. The integer source
``id`` is preserved as :attr:`GroundedSource.rank`, which is the anchor the
answer's inline ``[id]`` citations reference. Results are cached with a TTL
to avoid redundant API calls within one research run.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import logging
import threading
import weakref
from typing import Any, Mapping

from cachetools import TTLCache
from perplexity import (
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncPerplexity,
    RateLimitError,
)

from inqtrix.constants import SEARCH_TIMEOUT
from inqtrix.exceptions import AgentRateLimited
from inqtrix.providers.attempts import (
    AsyncNativeSearchProvider,
    call_with_retries,
    estimate_request_tokens,
)
from inqtrix.providers.base import (
    _apply_domain_filters,
    _bounded_timeout,
    _build_recency_language_hints,
    _operation_deadline,
)
from inqtrix.search_result import GroundedSearchResult, GroundedSource, ProviderNotice

log = logging.getLogger(__name__)


class PerplexitySearch(AsyncNativeSearchProvider):
    """Query the Perplexity Agent API and normalize the grounded result.

    Use this provider when web search should come from Perplexity's Agent
    API. One call returns both a synthesized, inline-cited answer and the
    structured per-source ``search_results`` that back it, so the evidence
    pipeline gets rich per-source snippets and a ready cross-matched
    synthesis in a single round trip.

    Attributes:
        _clients (WeakKeyDictionary): One ``AsyncPerplexity`` client per
            event loop, built on first use from ``_client_kwargs``.
        _model (str): Agent reasoning model (e.g. ``"sonar"``).
        _preset (str | None): Optional Agent preset (e.g. a fast-search
            preset) selected instead of an explicit model.
        _instructions (str | None): Optional system instructions passed to
            the Agent.
        _cache (TTLCache): In-memory cache of normalized search results.
        _cache_lock (threading.Lock): Lock guarding cache access for
            multi-threaded search runs.
        _request_params (dict[str, Any]): Extra request parameters merged
            into the SDK call after reserved keys are filtered out.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        preset: str = "fast-search",
        model: str | None = None,
        instructions: str | None = None,
        cache_maxsize: int = 256,
        cache_ttl: int = 3600,
        request_params: dict[str, Any] | None = None,
        timeout: float = SEARCH_TIMEOUT,
        default_headers: Mapping[str, str] | None = None,
        # Internal: accept a pre-built async client from tests.
        _client: AsyncPerplexity | None = None,
    ) -> None:
        """Initialize the Perplexity Agent API provider.

        Args:
            api_key: API key for the Perplexity API.
            base_url: Optional API base URL override. Leave ``None`` to use
                the SDK default (``https://api.perplexity.ai``); set it only
                to target a Perplexity-compatible proxy.
            preset: Agent preset that drives the search. The default
                ``"fast-search"`` works out of the box: it bundles web search
                and the system prompt that yields inline ``[n]`` citations.
                Other values: ``"pro-search"``, ``"deep-research"``.
            model: Optional explicit Agent model (e.g. ``"perplexity/sonar"``,
                ``"openai/gpt-5.5"``). When set it overrides ``preset`` and you
                are responsible for citation behaviour via ``instructions``.
            instructions: Optional system instructions for the Agent.
            cache_maxsize: Maximum number of normalized search responses
                kept in the in-memory cache. The default is ``256``.
            cache_ttl: Cache time-to-live in seconds. The default is
                ``3600``.
            request_params: Optional extra SDK parameters merged into each
                ``responses.create`` call after reserved keys are filtered.
            timeout: Budget in seconds for one complete logical search,
                including every visible retry and backoff.
            default_headers: Optional headers sent with every request,
                for example the subscription header of an API gateway.
            _client: Optional prebuilt async SDK client used by tests on
                every loop.

        Example:
            >>> from inqtrix import PerplexitySearch
            >>> search = PerplexitySearch(api_key="test-key")  # fast-search preset
            >>> search.is_available()
            True
        """
        self._prebuilt_client = _client
        self._client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
        }
        if base_url:
            self._client_kwargs["base_url"] = base_url
        if default_headers:
            self._client_kwargs["default_headers"] = dict(default_headers)
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, AsyncPerplexity
        ] = weakref.WeakKeyDictionary()
        self._model = model
        self._preset = preset
        self._instructions = instructions
        self._cache: TTLCache = TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)
        self._cache_lock = threading.Lock()
        self._request_params = dict(request_params or {})
        self._timeout = float(timeout)

    def _async_client(self) -> AsyncPerplexity:
        """The SDK client of the running loop: one per provider and loop, built on first use."""
        if self._prebuilt_client is not None:
            return self._prebuilt_client
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = AsyncPerplexity(**self._client_kwargs)
            self._clients[loop] = client
        return client

    # -- public interface --------------------------------------------------

    async def asearch(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> GroundedSearchResult:
        """Execute a Perplexity Agent API search and normalize the response.

        Args:
            query: User-facing search query text.
            search_context_size: Accepted for ABC compatibility; the Agent
                decides its own context depth, so this value is not used.
            recency_filter: Optional freshness hint, applied as best-effort
                prompt guidance prepended to the query.
            language_filter: Optional language hints, applied as best-effort
                prompt guidance.
            domain_filter: Optional allow/deny domain list, injected as
                ``site:`` / ``-site:`` operators into the query text.
            search_mode: Accepted for ABC compatibility; not used.
            return_related: Accepted for ABC compatibility; the Agent API
                response does not surface related questions in this shape.
            deadline: Optional absolute monotonic deadline for the run.

        Returns:
            GroundedSearchResult: The synthesized answer plus per-source
            records with full snippets. Cached results are deep-copied.

        Raises:
            AgentRateLimited: If the backend returns HTTP 429 or the SDK
                raises ``RateLimitError``.
        """
        cache_parts = [
            query,
            str(recency_filter),
            str(language_filter),
            str(domain_filter),
            str(self._model),
            str(self._preset),
        ]
        key = hashlib.sha256("|".join(cache_parts).encode()).hexdigest()
        with self._cache_lock:
            cached = self._cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)

        effective_query = _apply_domain_filters(query, domain_filter)
        hint = _build_recency_language_hints(recency_filter, language_filter)
        user_input = f"{hint}\n\n{effective_query}" if hint else effective_query

        operation_deadline = _operation_deadline(self._timeout, deadline)
        create_kwargs: dict[str, Any] = {
            "input": user_input,
            "tools": [{"type": "web_search"}],
            "stream": False,
        }
        # An explicit model wins; otherwise the preset (default "fast-search")
        # drives the agent and bundles the inline-citation system prompt.
        if self._model:
            create_kwargs["model"] = self._model
        else:
            create_kwargs["preset"] = self._preset
        if self._instructions:
            create_kwargs["instructions"] = self._instructions
        for param_key, param_val in self._request_params.items():
            if param_key in {"input", "tools", "stream"}:
                continue
            create_kwargs[param_key] = param_val

        client = self._async_client()
        try:
            response = await call_with_retries(
                lambda: client.responses.create(
                    **{
                        **create_kwargs,
                        "timeout": _bounded_timeout(
                            self._timeout, operation_deadline
                        ),
                    }
                ),
                provider_label=type(self).__name__,
                model=self.search_model,
                operation="web_search",
                deadline=operation_deadline,
                outer_deadline=deadline,
                timeout_label="Perplexity-WebSearch",
                configured_timeout_seconds=self._timeout,
                kind="search",
                estimated_input_tokens=estimate_request_tokens(create_kwargs),
                max_output_tokens=int(create_kwargs.get("max_output_tokens") or 0) or None,
            )
        except RateLimitError as exc:
            log.error("FATAL Rate-Limit in Perplexity-WebSearch")
            raise AgentRateLimited(self._model, exc) from exc
        except APIStatusError as exc:
            if exc.status_code == 429:
                log.error("FATAL Rate-Limit in Perplexity-WebSearch")
                raise AgentRateLimited(self._model, exc) from exc
            log.error(
                "Perplexity-WebSearch failed (status=%s, type=%s)",
                exc.status_code,
                type(exc).__name__,
            )
            return GroundedSearchResult(notices=(ProviderNotice(
                "Perplexity-WebSearch failed; the empty result remains "
                "a visible evidence gap.",
                code=(
                    "provider_timeout"
                    if exc.status_code == 408
                    else (
                        "upstream_5xx"
                        if exc.status_code >= 500
                        else "provider_error"
                    )
                ),
                http_status=exc.status_code,
            ),))
        except APITimeoutError:
            log.error("Perplexity-WebSearch reached the provider timeout")
            return GroundedSearchResult(notices=(ProviderNotice(
                "Perplexity-WebSearch reached the provider timeout; "
                "the empty result remains a visible evidence gap.",
                code="provider_timeout",
                http_status=504,
            ),))
        except APIError as exc:
            log.error(
                "Perplexity-WebSearch failed (type=%s)",
                type(exc).__name__,
            )
            return GroundedSearchResult(notices=(ProviderNotice(
                "Perplexity-WebSearch failed; the empty result remains "
                "a visible evidence gap.",
                code="temporary_transport",
                http_status=503,
            ),))

        result = self._parse_response(response)
        # The cache keeps the result only: a later hit is no new empty answer.
        with self._cache_lock:
            self._cache[key] = result
        result = copy.deepcopy(result)
        if not result.answer and not result.sources:
            result = dataclasses.replace(result, notices=(ProviderNotice(
                "Perplexity-WebSearch returned neither answer text nor sources."
            ),))
        return result

    @staticmethod
    def _parse_response(response: Any) -> GroundedSearchResult:
        """Extract the answer and per-source records from an Agent response."""
        answer = ""
        sources: list[GroundedSource] = []
        for item in getattr(response, "output", []) or []:
            item_type = getattr(item, "type", None)
            if item_type == "search_results":
                for result in getattr(item, "results", []) or []:
                    url = str(getattr(result, "url", "") or "").strip()
                    if not url:
                        continue
                    raw_id = getattr(result, "id", 0)
                    try:
                        rank = int(raw_id)
                    except (TypeError, ValueError):
                        rank = len(sources) + 1
                    sources.append(
                        GroundedSource(
                            url=url,
                            title=str(getattr(result, "title", "") or ""),
                            snippet=str(getattr(result, "snippet", "") or ""),
                            date=str(getattr(result, "date", "") or ""),
                            last_updated=str(getattr(result, "last_updated", "") or ""),
                            rank=rank,
                            origin="search_results",
                        )
                    )
            elif item_type == "message" and not answer:
                parts = [
                    str(getattr(part, "text", "") or "")
                    for part in getattr(item, "content", []) or []
                    if getattr(part, "type", "output_text") == "output_text"
                ]
                answer = "".join(parts).strip()

        if not answer:
            answer = str(getattr(response, "output_text", "") or "").strip()

        usage = getattr(response, "usage", None)
        return GroundedSearchResult(
            answer=answer,
            sources=sources,
            prompt_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Returns:
            bool: ``True`` when a client is given or its configuration was
            resolved; the client itself is built per loop on first use.
        """
        return self._prebuilt_client is not None or bool(self._client_kwargs)

    @property
    def search_model(self) -> str:
        """Operator-facing identifier of the Agent backend in use.

        The constructor-supplied model (when set) or preset identifier,
        surfaced in ``GET /health`` and ``GET /v1/stacks``.
        """
        return self._model or self._preset
