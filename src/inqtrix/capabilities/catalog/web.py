"""Web capability: a single grounded search call (no research graph).

``web.search.instant`` is the smallest external-information tool: ONE
:class:`~inqtrix.providers.base.SearchProvider` call, returning the
provider's grounded answer and sources as citable references — no
LangGraph research run, no child run. It is the discovery-preview and
small-gap tool; a full report goes through ``research``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from inqtrix import aio
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
from inqtrix.search_form import (
    ANSWER_FORM_PARAMETER,
    ANSWER_FORMS,
    DEFAULT_ANSWER_FORM,
)
from inqtrix.search_result import GroundedSearchResult
from inqtrix.providers.attempts import observe_retries
from inqtrix.providers.base import get_search_provider_capabilities
from inqtrix.observability.context import bind_purpose

if TYPE_CHECKING:
    from inqtrix.providers.base import SearchProvider

log = logging.getLogger(__name__)

_RECENCY = {"", "day", "week", "month", "year"}


class WebInstantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1)
    recency: str = Field("", description="'', 'day', 'week', 'month', 'year'")
    max_sources: int = Field(8, ge=1, le=20)
    answer_form: str = Field(
        DEFAULT_ANSWER_FORM,
        description="'overview' (the default, sends nothing), 'table', 'timeline'",
    )


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
        if payload.answer_form not in ANSWER_FORMS:
            raise CapabilityError(
                "invalid_input",
                "answer_form must be 'overview', 'table' or 'timeline'.",
                http_status=400,
            )
        # The default form asks for nothing. Any other form is asked of a
        # provider that names the parameter, and of no other: the keyword
        # never reaches a provider that does not know it.
        form_requested = payload.answer_form != DEFAULT_ANSWER_FORM

        # The search runs through its sync face, off the loop: the retry
        # callback is bound on this thread, and the sync face waits for the
        # process loop.
        def _search_with_notice() -> tuple[GroundedSearchResult, object, bool]:
            active_search = context.search_provider or search_provider
            form_sent = form_requested and get_search_provider_capabilities(
                active_search
            ).supports(ANSWER_FORM_PARAMETER)
            if form_requested and not form_sent:
                log.info(
                    "Web search runs without the requested answer form %s: "
                    "the search provider %s names no answer form.",
                    payload.answer_form,
                    _provider_label(active_search),
                )
            callback = None
            if context.on_provider_retry is not None:
                callback = lambda notice: context.on_provider_retry({
                    **notice,
                    "operation": "web.search.instant",
                    "query": payload.query,
                })
            with bind_purpose("web_instant"), observe_retries("search", callback):
                result = active_search.search(
                    payload.query,
                    search_context_size="low",
                    recency_filter=payload.recency or None,
                    **({"answer_form": payload.answer_form} if form_sent else {}),
                )
            notice = next((item.as_detail() for item in result.notices), None)
            return result, notice, form_sent

        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.perf_counter()
        result, notice, form_sent = await aio.run_provider_blocking(_search_with_notice)
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
        # The id decides which record a search is. Without a form it is
        # what it always was. The same question in another form is another
        # search, whether or not the provider was handed the form: its id
        # is made from the id without a form, so no question text can
        # stand in for a form.
        query_id = make_record_id(
            "query",
            context.run_id
            or make_record_id("run", "web.search.instant", payload.query),
            payload.query,
        )
        if form_requested:
            query_id = make_record_id("query", query_id, payload.answer_form)
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
                # What was asked for and whether it was handed to the
                # provider: one that names no answer form searched without
                # it. Whether the answer follows the form, nobody checks.
                **(
                    {
                        "answer_form": payload.answer_form,
                        "answer_form_sent": form_sent,
                    }
                    if form_requested
                    else {}
                ),
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
