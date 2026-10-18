"""Provider-neutral search output.

This module defines the single typed contract between any
:class:`~inqtrix.providers.base.SearchProvider` and the evidence pipeline.
It is intentionally a dependency-free leaf module (no imports from other
``inqtrix`` modules) so that the providers, the runtime-logging layer, the
evidence assembly, and the graph nodes can all share it without import
cycles.

Data boundaries:

* No character caps live here. A :class:`GroundedSource` carries the full
  provider snippet; truncation, if any, happens only once, visibly, at the
  final answer-prompt render.
* There is no ``granularity`` field. Whether a source carries its own body
  (Perplexity ``search_results``) or only a citation URL (Azure Foundry,
  whose answer is one synthesized block) follows from whether
  :attr:`GroundedSource.snippet` is populated -- the evidence assembly does
  not branch on a provider-declared mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class CitationSpan:
    """One place of the provider answer that cites a source: provider-native
    offsets into that answer (``end`` exclusive), never into the linked
    webpage."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class GroundedSource:
    """One retrieved web source with its full, untruncated content.

    Attributes:
        url: Source URL exactly as the provider reported it. Normalization
            to a canonical form happens downstream in
            ``normalize_source_provenance``; this field stays raw.
        title: Human-readable source title, or empty when the backend does
            not supply one.
        snippet: The provider's per-source body text, kept in full. Rich for
            Perplexity ``search_results`` (often thousands of characters);
            empty for Azure Foundry, whose API exposes no per-source body.
        date: Publication date string as reported, or empty.
        last_updated: Last-updated date string as reported, or empty.
        rank: Provider-local 1-based ordinal. For Perplexity this is the
            integer ``id`` that the synthesized answer's inline ``[id]``
            citations reference -- preserving it here is what lets the
            inline cross-matching feed claim source-binding downstream.
        origin: Lineage marker for where the URL came from, e.g.
            ``"search_results"``, ``"url_citation"``, ``"markdown_link"``,
            or ``"answer_url_fallback"``.
        spans: Every place of the provider answer that cites this source,
            in answer order. A source is one per URL, but an answer may cite
            it many times, each time for a different statement; each span
            becomes its own citation record downstream. Empty when the
            provider gives no offsets (a link found in the text, Perplexity).
    """

    url: str
    title: str = ""
    snippet: str = ""
    date: str = ""
    last_updated: str = ""
    rank: int = 0
    origin: str = ""
    spans: tuple[CitationSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class GroundedSearchResult:
    """Normalized result every :class:`SearchProvider` returns.

    Attributes:
        answer: The provider's synthesized answer text in full. May contain
            inline citation markers (Perplexity ``[id]`` or Azure Foundry
            ``([domain](url))``); those are neutralized to plain text before
            the synthesis reaches the final answer LLM.
        sources: Per-source records. Rich for Perplexity (one per
            ``search_results`` entry, with full snippet); URL-only for Azure
            Foundry (one per cited URL, no snippet).
        related_questions: Optional provider-suggested related questions.
            Preserved for compatibility; the research loop does not use
            them for planning or answer synthesis.
        prompt_tokens: Input token count reported by the backend, or ``0``.
        completion_tokens: Output token count reported by the backend, or
            ``0``.
    """

    answer: str = ""
    sources: list[GroundedSource] = field(default_factory=list)
    related_questions: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    """Actual model reported by a hosted search agent, when available."""
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_call_count: int | None = None
    """Reported web-tool calls, not an assertion about billable transactions."""
    raw: dict[str, Any] | None = field(default=None, repr=False)
    """Provider response for protected trace content; not evidence prose."""

    @property
    def citation_urls(self) -> list[str]:
        """Return the ordered, de-duplicated list of source URLs.

        Compatibility helper for the few call sites that only need the bare
        URL list rather than the full per-source records.

        Returns:
            list[str]: Source URLs in source order, first occurrence kept.
        """
        out: list[str] = []
        for source in self.sources:
            if source.url and source.url not in out:
                out.append(source.url)
        return out
