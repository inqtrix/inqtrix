"""Web capability: a single grounded search call (no research graph).

``web.search.instant`` is the smallest external-information tool: ONE
:class:`~inqtrix.providers.base.SearchProvider` call, returning the
provider's grounded answer and sources as citable references — no
LangGraph research run, no child run. It is the discovery-preview and
small-gap tool; a full report goes through ``research``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    CapabilityError,
    Effect,
)
from inqtrix.runtime_logging import (
    make_record_id,
    sanitize_grounded_search_result,
)
from inqtrix.search_result import GroundedSearchResult
from inqtrix.providers.base import observe_provider_retries
from inqtrix.observability.context import bind_purpose

if TYPE_CHECKING:
    from inqtrix.providers.base import SearchProvider

_RECENCY = {"", "day", "week", "month", "year"}


class WebInstantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1)
    recency: str = Field("", description="'', 'day', 'week', 'month', 'year'")
    max_sources: int = Field(8, ge=1, le=20)


class WebCitationSpan(BaseModel):
    """One place of the provider answer that cites the source."""

    start: int
    end: int


class WebSource(BaseModel):
    url: str
    title: str
    snippet: str
    date: str
    rank: int
    last_updated: str = ""
    origin: str = ""
    spans: list[WebCitationSpan] = Field(default_factory=list)


class WebInstantOutput(BaseModel):
    query_id: str
    query: str
    provider: str
    answer: str
    sources: list[WebSource]
    parameters: dict[str, object] = Field(default_factory=dict)
    started_at: str
    finished_at: str
    duration_ms: int = Field(0, ge=0)
    prompt_tokens: int = Field(0, ge=0)
    completion_tokens: int = Field(0, ge=0)


def _provider_label(provider: object) -> str:
    """Return the concrete search adapter behind authorization decorators."""

    current = provider
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        wrapped = getattr(current, "_provider", None)
        if wrapped is None:
            break
        current = wrapped
    return type(current).__name__


def build_web_capabilities(
    search_provider: "SearchProvider",
) -> list[CapabilityDefinition]:
    """Build the web capability bound to *search_provider*."""

    async def _instant(
        payload: WebInstantInput, context: CapabilityContext
    ) -> WebInstantOutput:
        if payload.recency not in _RECENCY:
            raise CapabilityError(
                "invalid_input",
                "recency must be '', 'day', 'week', 'month' or 'year'.",
                http_status=400,
            )
        # SearchProvider.search is synchronous (blocking HTTP); keep it off
        # the event loop.
        def _search_with_notice() -> tuple[GroundedSearchResult, object]:
            active_search = context.search_provider or search_provider
            callback = None
            if context.on_provider_retry is not None:
                callback = lambda notice: context.on_provider_retry({
                    **notice,
                    "operation": "web.search.instant",
                    "query": payload.query,
                })
            with bind_purpose("web_instant"), observe_provider_retries(active_search, callback):
                result = active_search.search(
                    payload.query,
                    search_context_size="low",
                    recency_filter=payload.recency or None,
                )
            consumer = getattr(
                active_search, "consume_nonfatal_notice_detail", None
            )
            notice = consumer() if callable(consumer) else None
            return result, notice

        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.perf_counter()
        result, notice = await asyncio.to_thread(_search_with_notice)
        result = sanitize_grounded_search_result(result)
        finished_at = datetime.now(timezone.utc).isoformat()
        duration_ms = max(
            0,
            round((time.perf_counter() - started_monotonic) * 1000),
        )
        if isinstance(notice, dict) and notice.get("code"):
            raise CapabilityError(
                str(notice["code"]),
                str(notice.get("message") or "Web search failed."),
                http_status=int(notice.get("http_status") or 502),
            )
        all_sources = [
            WebSource(
                url=source.url,
                title=source.title,
                snippet=source.snippet,
                date=source.date,
                rank=source.rank or index,
                last_updated=source.last_updated,
                origin=source.origin,
                spans=[WebCitationSpan(start=span.start, end=span.end) for span in source.spans],
            )
            for index, source in enumerate(result.sources, start=1)
        ]
        provider = _provider_label(context.search_provider or search_provider)
        query_id = make_record_id(
            "query",
            context.run_id
            or make_record_id("run", "web.search.instant", payload.query),
            payload.query,
        )
        return WebInstantOutput(
            query_id=query_id,
            query=payload.query,
            provider=provider,
            answer=result.answer,
            # The complete provider source set is persisted for audit and
            # Canvas inspection. Callers may render a compact subset, but the
            # capability boundary never discards the remaining citations.
            sources=all_sources,
            parameters={
                "recency": payload.recency,
                "search_context_size": "low",
                "visible_source_limit": payload.max_sources,
            },
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )

    return [
        CapabilityDefinition(
            id="web.search.instant",
            summary="Run one grounded web search (no research graph).",
            input_model=WebInstantInput,
            output_model=WebInstantOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_instant,
        ),
    ]
