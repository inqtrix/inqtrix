"""EvidenceLedger helpers: assembly, claim projection, and the single
record-driven evidence overview rendered for the final answer prompt."""

from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from inqtrix.runtime_logging import make_record_id
from inqtrix.domains import placement_kind
from inqtrix.scoring import SUPPORTED_STATUSES
from inqtrix.urls import domain_from_url, normalize_url

# Evidence content (snippets, passages, claims, the per-query synthesis) is
# stored and rendered in full -- there are no per-field character caps. The
# only length bound left is the answer-prompt render budget
# (``max_total_chars`` / ``max_record_chars``), which truncates visibly with a
# marker and reports an omitted-record count.

# Trust ranking for evidence records when the overview character budget is
# tight. Low-tier sources sink to the bottom but are not hard-excluded.
_SOURCE_CONTEXT_TIER_RANK: dict[str, int] = {
    "primary": 60,
    "mainstream": 50,
    "stakeholder": 40,
    "unknown": 25,
    "low": -100,
}

_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_OVERVIEW_PATH_RE = re.compile(
    r"(^|/)(latest|category|categories|topics|tag|tags|events-calendar|ai-events|news)(/|$)",
    re.IGNORECASE,
)
_EVIDENCE_LABEL_RE = re.compile(r"^E(\d+)$")
_ADJACENT_CITATION_MARKER_RE = re.compile(
    r"(\[(?:E\d+|unmapped:[^\]]+|unrendered:[^\]]+)\])"
    r"(?=\[(?:E\d+|unmapped:|unrendered:))"
)


def evidence_id_for_citation(query_id: str, citation_record: dict[str, Any]) -> str:
    """Return the stable EvidenceRecord id for one citation record.

    Args:
        query_id: Query that produced the citation.
        citation_record: Normalized provider citation record.

    Returns:
        Stable EvidenceRecord identifier. The same query/citation/URL
        tuple always maps to the same id within and across runs.
    """
    canonical = normalize_url(str(citation_record.get("canonical_url", "") or ""))
    citation_id = str(citation_record.get("citation_id", "") or "")
    source_id = str(citation_record.get("source_id", "") or "")
    return make_record_id("ev", query_id, citation_id or source_id, canonical)


def build_web_search_ledger(
    *,
    run_id: str,
    query_records: list[dict[str, Any]],
    query_synthesis: Mapping[str, dict[str, Any]],
    citation_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project provider-visible search results into one read-only UI ledger.

    This is deliberately not a second evidence or validation system. It
    serializes the exact search request metadata, provider answer and provider
    citations already stored by the Research Desk so an answer reference can
    open the producing search in the Canvas. No URL is fetched here.
    """

    citations_by_query: dict[str, list[dict[str, Any]]] = {}
    for raw in citation_records:
        if not isinstance(raw, dict):
            continue
        query_id = str(raw.get("query_id") or "")
        if not query_id:
            continue
        canonical_url = normalize_url(
            str(raw.get("canonical_url") or raw.get("url") or "")
        )
        if not canonical_url:
            continue
        citations_by_query.setdefault(query_id, []).append(
            {
                "citation_id": str(raw.get("citation_id") or ""),
                "source_id": str(raw.get("source_id") or ""),
                "url": canonical_url,
                "title": str(raw.get("title") or ""),
                "snippet": str(raw.get("snippet") or ""),
                "rank": max(0, int(raw.get("rank") or 0)),
                "origin": str(raw.get("origin") or ""),
                "source_date": str(raw.get("source_date") or ""),
                "last_updated": str(raw.get("last_updated") or ""),
                "annotation_start": _optional_nonnegative_int(
                    raw.get("annotation_start")
                ),
                "annotation_end": _optional_nonnegative_int(
                    raw.get("annotation_end")
                ),
            }
        )

    searches: dict[str, dict[str, Any]] = {}
    for raw in query_records:
        if not isinstance(raw, dict):
            continue
        query_id = str(raw.get("query_id") or raw.get("invocation_id") or "")
        if not query_id:
            continue
        synthesis = query_synthesis.get(query_id, {})
        provider_answer = str(synthesis.get("provider_answer") or "")
        citations = _with_provider_answer_context(
            provider_answer,
            citations_by_query.get(query_id, []),
        )
        searches[query_id] = {
            "query_id": query_id,
            "invocation_id": str(raw.get("invocation_id") or query_id),
            "source_run_id": run_id,
            "round": max(0, int(raw.get("round") or 0)),
            "query_index": max(0, int(raw.get("query_index") or 0)),
            "query": str(raw.get("query") or synthesis.get("query") or ""),
            "provider": str(raw.get("provider") or ""),
            "parameters": dict(raw.get("parameters") or {}),
            "status": str(raw.get("status") or "completed"),
            "notice": str(raw.get("notice") or ""),
            "started_at": str(raw.get("started_at") or ""),
            "finished_at": str(raw.get("finished_at") or ""),
            "duration_ms": max(0, int(raw.get("duration_ms") or 0)),
            "usage": dict(raw.get("usage") or {}),
            "provider_answer": provider_answer,
            "citations": citations,
        }

    # Legacy result states can carry synthesis/citations but no invocation
    # records. Preserve them instead of making their provider answer disappear.
    for query_id, synthesis in query_synthesis.items():
        key = str(query_id or "")
        if not key or key in searches:
            continue
        provider_answer = str(synthesis.get("provider_answer") or "")
        searches[key] = {
            "query_id": key,
            "invocation_id": key,
            "source_run_id": run_id,
            "round": max(0, int(synthesis.get("round") or 0)),
            "query_index": 0,
            "query": str(synthesis.get("query") or ""),
            "provider": "",
            "parameters": {},
            "status": "completed",
            "notice": "",
            "started_at": "",
            "finished_at": "",
            "duration_ms": 0,
            "usage": {
                "prompt_tokens": max(
                    0, int(synthesis.get("prompt_tokens") or 0)
                ),
                "completion_tokens": max(
                    0, int(synthesis.get("completion_tokens") or 0)
                ),
            },
            "provider_answer": provider_answer,
            "citations": _with_provider_answer_context(
                provider_answer,
                citations_by_query.get(key, []),
            ),
        }

    return {
        "schema_version": 1,
        "kind": "web_search_ledger",
        "searches": searches,
    }


def build_instant_web_search_ledger(
    *,
    run_id: str,
    query_id: str,
    query: str,
    provider: str,
    answer: str,
    sources: list[dict[str, Any]],
    parameters: Mapping[str, Any] | None = None,
    started_at: str = "",
    finished_at: str = "",
    duration_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict[str, Any]:
    """Build the same ledger shape for one direct provider search."""

    citation_records: list[dict[str, Any]] = []
    for index, source in enumerate(sources, start=1):
        url = normalize_url(str(source.get("url") or ""))
        if not url:
            continue
        source_id = make_record_id("src", url)
        rank = max(1, int(source.get("rank") or index))
        origin = source.get("origin") or "provider"
        spans = [
            (start, end)
            for span in source.get("spans") or []
            if isinstance(span, Mapping)
            and (start := _optional_nonnegative_int(span.get("start"))) is not None
            and (end := _optional_nonnegative_int(span.get("end"))) is not None
        ]
        # One citation per place of the answer that cites the source, as the
        # research ledger records it (``normalize_source_provenance``).
        for span in spans or [None]:
            citation_records.append(
                {
                    "citation_id": (
                        make_record_id("cit", query_id, rank, url, origin)
                        if span is None
                        else make_record_id("cit", query_id, rank, url, origin, *span)
                    ),
                    "query_id": query_id,
                    "source_id": source_id,
                    "url": url,
                    "canonical_url": url,
                    "rank": rank,
                    "origin": str(origin),
                    "title": str(source.get("title") or ""),
                    "snippet": str(source.get("snippet") or ""),
                    "source_date": str(source.get("date") or ""),
                    "last_updated": str(source.get("last_updated") or ""),
                    "annotation_start": span[0] if span else None,
                    "annotation_end": span[1] if span else None,
                }
            )
    return build_web_search_ledger(
        run_id=run_id,
        query_records=[
            {
                "query_id": query_id,
                "invocation_id": query_id,
                "round": 0,
                "query_index": 0,
                "query": query,
                "provider": provider,
                "parameters": dict(parameters or {}),
                "status": "completed",
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": max(0, int(duration_ms or 0)),
                "usage": {
                    "prompt_tokens": max(0, int(prompt_tokens or 0)),
                    "completion_tokens": max(
                        0, int(completion_tokens or 0)
                    ),
                },
            }
        ],
        query_synthesis={
            query_id: {
                "query": query,
                "round": 0,
                "provider_answer": answer,
                "prompt_tokens": max(0, int(prompt_tokens or 0)),
                "completion_tokens": max(0, int(completion_tokens or 0)),
            }
        },
        citation_records=citation_records,
    )


def merge_web_search_ledgers(
    ledgers: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    """Merge provider-search records without filtering their content."""

    searches: dict[str, dict[str, Any]] = {}
    for ledger in ledgers:
        raw_searches = ledger.get("searches", {}) if isinstance(ledger, dict) else {}
        if not isinstance(raw_searches, dict):
            continue
        for query_id, raw in raw_searches.items():
            if not isinstance(raw, dict):
                continue
            key = str(query_id or raw.get("query_id") or "")
            if not key:
                continue
            existing = searches.get(key)
            if existing is None:
                searches[key] = dict(raw)
                continue
            # Replays may add timing or source rows. Merge only additive
            # information; an existing provider answer is never replaced by a
            # later empty or differently summarized value.
            merged = dict(existing)
            for field_name, value in raw.items():
                if field_name == "citations":
                    continue
                if merged.get(field_name) in (None, "", [], {}):
                    merged[field_name] = value
            citations: OrderedDict[str, dict[str, Any]] = OrderedDict()
            for citation in [
                *(existing.get("citations", []) or []),
                *(raw.get("citations", []) or []),
            ]:
                if not isinstance(citation, dict):
                    continue
                identity = str(citation.get("citation_id") or "") or normalize_url(
                    str(citation.get("url") or "")
                )
                if identity:
                    citations[identity] = dict(citation)
            merged["citations"] = list(citations.values())
            searches[key] = merged
    return {
        "schema_version": 1,
        "kind": "web_search_ledger",
        "searches": searches,
    }


def project_web_search_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose the provider-search ledger without unrelated stored material.

    The search records retain their complete API answers and citation metadata.
    Other ledger collections are not part of the web-search evidence contract.
    Projection leaves the stored payload unchanged.
    """
    result = dict(payload)
    ledger = result.get("web_search_ledger")
    if isinstance(ledger, dict):
        result["web_search_ledger"] = merge_web_search_ledgers([ledger])
    return result


def attach_web_search_lineage(
    references: list[dict[str, Any]],
    ledger: dict[str, Any],
) -> list[dict[str, Any]]:
    """Attach stable search/citation pointers to final web references."""

    by_url: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {}
    searches = ledger.get("searches", {}) if isinstance(ledger, dict) else {}
    if isinstance(searches, dict):
        for query_id, search in searches.items():
            if not isinstance(search, dict):
                continue
            for citation in search.get("citations", []) or []:
                if not isinstance(citation, dict):
                    continue
                url = normalize_url(str(citation.get("url") or ""))
                if url:
                    by_url.setdefault(url, []).append(
                        (str(query_id), search, citation)
                    )

    projected: list[dict[str, Any]] = []
    for raw in references:
        ref = dict(raw)
        if ref.get("document_id") is not None:
            projected.append(ref)
            continue
        url = normalize_url(str(ref.get("url") or ""))
        matches = by_url.get(url, [])
        if matches:
            query_ids = list(dict.fromkeys(item[0] for item in matches))
            citation_ids = list(
                dict.fromkeys(
                    str(item[2].get("citation_id") or "")
                    for item in matches
                    if str(item[2].get("citation_id") or "")
                )
            )
            source_run_ids = list(
                dict.fromkeys(
                    str(item[1].get("source_run_id") or "")
                    for item in matches
                    if str(item[1].get("source_run_id") or "")
                )
            )
            ref["query_id"] = query_ids[0]
            ref["query_ids"] = query_ids
            if citation_ids:
                ref["citation_id"] = citation_ids[0]
                ref["citation_ids"] = citation_ids
            if source_run_ids:
                ref["source_run_id"] = source_run_ids[0]
                ref["source_run_ids"] = source_run_ids
            primary_citation = matches[0][2]
            source_id = str(primary_citation.get("source_id") or "")
            if source_id:
                ref["source_id"] = source_id
            support = str(primary_citation.get("grounded_support") or "")
            if support and not ref.get("grounded_support"):
                ref["grounded_support"] = support
            if not ref.get("provider_snippet") and primary_citation.get("snippet"):
                ref["provider_snippet"] = str(primary_citation["snippet"])
        projected.append(ref)
    return projected


def _with_provider_answer_context(
    provider_answer: str,
    citations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add honest provider-answer context without inventing URL attribution."""

    if not citations:
        return []
    # Reuse the established deterministic URL-to-answer-context helper. It
    # removes the URL from the displayed passage and never claims that the
    # passage is a verbatim excerpt from the linked webpage.
    from inqtrix.agents.evidence import enrich_instant_evidence

    enriched = enrich_instant_evidence(
        provider_answer,
        [
            {
                "url": citation.get("url"),
                "title": citation.get("title"),
            }
            for citation in citations
        ],
    )
    projected: list[dict[str, Any]] = []
    for citation, support in zip(citations, enriched, strict=True):
        row = dict(citation)
        annotation_support = _annotation_support(provider_answer, citation)
        nearby_support = str(support.get("grounded_support") or "")
        if annotation_support and _is_citation_marker(
            annotation_support,
            str(citation.get("url") or ""),
        ):
            # Azure commonly annotates only the rendered Markdown link, not
            # the proposition before it. Preserve useful nearby context while
            # recording that the provider did not expose a 1:1 claim mapping.
            row["grounded_support"] = (
                _citation_marker_context(provider_answer, citation)
                or nearby_support
                or annotation_support
            )
            row["mapping_status"] = "provider_citation_marker"
        elif annotation_support:
            row["grounded_support"] = annotation_support
            row["mapping_status"] = "provider_answer_context"
        elif nearby_support:
            row["grounded_support"] = nearby_support
            row["mapping_status"] = "provider_citation_marker"
        elif row.get("snippet"):
            row["mapping_status"] = "provider_snippet"
        else:
            row["mapping_status"] = "source_only"
        projected.append(row)
    return projected


def _annotation_support(
    provider_answer: str,
    citation: Mapping[str, Any],
) -> str:
    """Return the exact provider-answer span attached to one citation."""

    start = _optional_nonnegative_int(citation.get("annotation_start"))
    end = _optional_nonnegative_int(citation.get("annotation_end"))
    if (
        start is None
        or end is None
        or end <= start
        or end > len(provider_answer)
    ):
        return ""
    from inqtrix.agents.evidence import shorten_grounded_support

    return shorten_grounded_support(provider_answer[start:end])


def _is_citation_marker(annotation: str, url: str) -> bool:
    """Return whether a provider-native span contains only a link marker."""

    cleaned = _MARKDOWN_LINK_RE.sub("", annotation)
    target = normalize_url(url)
    cleaned = re.sub(
        r"https?://[^\s)\]}>,\"']+",
        lambda match: (
            ""
            if normalize_url(match.group(0)) == target
            else match.group(0)
        ),
        cleaned,
    )
    words = re.findall(r"[A-Za-zÄÖÜäöüß0-9$€£%]+", cleaned)
    return len(words) < 3


def _citation_marker_context(
    provider_answer: str,
    citation: Mapping[str, Any],
) -> str:
    """Return the answer line surrounding an Azure citation marker."""

    start = _optional_nonnegative_int(citation.get("annotation_start"))
    end = _optional_nonnegative_int(citation.get("annotation_end"))
    if start is None or end is None or end <= start:
        return ""
    line_start = provider_answer.rfind("\n", 0, start) + 1
    line_end = provider_answer.find("\n", end)
    if line_end < 0:
        line_end = len(provider_answer)
    context = provider_answer[line_start:line_end].strip()
    context = _MARKDOWN_LINK_RE.sub("", context)
    target = normalize_url(str(citation.get("url") or ""))
    context = re.sub(
        r"https?://[^\s)\]}>,\"']+",
        lambda match: (
            ""
            if normalize_url(match.group(0)) == target
            else match.group(0)
        ),
        context,
    )
    context = re.sub(r"^[ \t]*(?:[-*+] |#+ |>|\d+[.)] )", "", context)
    context = " ".join(context.split()).strip(" ()-–—")
    from inqtrix.agents.evidence import shorten_grounded_support

    return shorten_grounded_support(context)


def _optional_nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _bounded_text(text: Any, limit: int) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


def _full_text(text: Any) -> str:
    """Whitespace-normalized full text on a single line, with no length cap.

    Used for evidence content fields (claims, snippets, passages). The only
    length bound left is the answer-prompt render budget, which truncates
    visibly with a marker.
    """
    return " ".join(str(text or "").split())


def _full_block(text: Any) -> str:
    """Full block text that preserves paragraph/bullet newlines, no cap.

    Used for the per-query synthesis (summary and provider answer) where
    newlines carry list structure. Collapses only runs of spaces/tabs and
    excess blank lines.
    """
    raw = str(text or "")
    if not raw.strip():
        return ""
    normalized = re.sub(r"[ \t]+", " ", raw).strip()
    return re.sub(r"\n{3,}", "\n\n", normalized)


_NUMERIC_CITATION_RE = re.compile(r"\[(?:web:)?(\d+)\]")


def _space_adjacent_evidence_labels(text: str) -> str:
    """Insert a space between adjacent evidence/citation diagnostic markers."""
    return _ADJACENT_CITATION_MARKER_RE.sub(r"\1 ", text)


def _remap_synthesis_citations(
    text: str,
    url_to_label: dict[str, str],
    rank_to_url: dict[str, str],
) -> str:
    """Rewrite a provider answer's inline citations to global ``[E#]`` labels.

    Maps Markdown-link citations (Azure ``([title](url))``) and numeric
    markers (Perplexity ``[2]`` / ``[web:2]``) to the stable evidence label
    of the cited source, so the synthesis cites the same ``[E#]`` labels as
    the rendered source blocks and carries no token-heavy inline URLs. The
    final answer LLM therefore only ever sees compact labels; the URLs are
    re-attached after synthesis. Citations that cannot be mapped are kept as
    visible diagnostic markers instead of disappearing silently. Numeric
    provider refs whose URL exists but whose source block is not visible
    become ``[unrendered:n]`` so they cannot be mistaken for valid
    evidence labels.

    Args:
        text: The provider's raw synthesized answer.
        url_to_label: Canonical URL to ``E#`` label for the query's sources.
        rank_to_url: Provider source rank/id to URL (Perplexity numeric ids).

    Returns:
        str: The answer with inline citations rewritten to ``[E#]`` labels.
    """
    if not text:
        return ""

    def _label_for_url(url: str) -> str | None:
        return url_to_label.get(normalize_url(str(url or "")))

    def _md(match: re.Match[str]) -> str:
        label = _label_for_url(match.group(2))
        return f"[{label}]" if label else f"{match.group(1)} [unrendered:url]"

    out = _MARKDOWN_LINK_RE.sub(_md, text)

    def _num(match: re.Match[str]) -> str:
        ref = match.group(1)
        url = rank_to_url.get(ref)
        if not url:
            return f"[unmapped:{ref}]"
        label = _label_for_url(url) if url else None
        return f"[{label}]" if label else f"[unrendered:{ref}]"

    out = _NUMERIC_CITATION_RE.sub(_num, out)
    out = re.sub(r"\(\s*(\[E\d+\])\s*\)", r"\1", out)
    out = re.sub(r"[ \t]+([.,;:)])", r"\1", out)
    out = _space_adjacent_evidence_labels(out)
    return out


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _passages_for_record(
    *,
    evidence_id: str,
    citation_record: dict[str, Any],
    claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    passages: list[dict[str, Any]] = []

    def add(origin: str, text: Any) -> None:
        bounded = _full_text(text)
        if not bounded:
            return
        if any(p["text"] == bounded for p in passages):
            return
        passages.append(
            {
                "passage_id": make_record_id("passage", evidence_id, origin, len(passages) + 1),
                "origin": origin,
                "text": bounded,
                "char_count": len(bounded),
            }
        )

    add("source_snippet", citation_record.get("snippet", ""))
    for claim in claims:
        add("claim_evidence_snippet", claim.get("evidence_snippet", ""))
    return passages


def _citation_set_for_records(citation_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    citation_set: list[dict[str, Any]] = []
    for index, citation in enumerate(citation_records, start=1):
        citation_set.append(
            {
                "label": f"E1.{index}",
                "evidence_id": "",
                "citation_id": citation.get("citation_id", ""),
                "source_id": citation.get("source_id", ""),
                "url": normalize_url(str(citation.get("canonical_url", "") or "")),
                "role": "source",
                "title": citation.get("title", ""),
                "snippet": citation.get("snippet", ""),
            }
        )
    return citation_set


def _claim_citation_set(
    claim: dict[str, Any],
    citation_records: list[dict[str, Any]],
    *,
    evidence_id: str,
    allow_overview_without_snippet: bool = False,
) -> list[dict[str, Any]]:
    claim_urls = {
        normalize_url(str(url))
        for url in claim.get("source_urls", []) or []
        if normalize_url(str(url))
    }
    if not claim_urls:
        return []
    rows = []
    for index, citation in enumerate(citation_records, start=1):
        url = normalize_url(str(citation.get("canonical_url", "") or ""))
        if claim_urls and url not in claim_urls:
            continue
        if (
            not allow_overview_without_snippet
            and _is_overview_url(url)
            and not str(citation.get("snippet", "")).strip()
        ):
            continue
        rows.append(
            {
                "label": f"E1.{index}",
                "evidence_id": evidence_id,
                "citation_id": citation.get("citation_id", ""),
                "source_id": citation.get("source_id", ""),
                "url": url,
                "role": "source",
                "title": citation.get("title", ""),
                "snippet": citation.get("snippet", ""),
            }
        )
    return rows


def _is_overview_url(url: str) -> bool:
    try:
        path = urlparse(url).path or ""
    except ValueError:
        return False
    return bool(_OVERVIEW_PATH_RE.search(path.rstrip("/")))


def _claim_payload(
    claim: dict[str, Any],
    *,
    citation_set: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "raw_claim_id": claim.get("raw_claim_id", ""),
        "claim_text": claim.get("claim_text", ""),
        "claim_type": claim.get("claim_type", "fact"),
        "polarity": claim.get("polarity", "affirmed"),
        "needs_primary": bool(claim.get("needs_primary", False)),
        "evidence_snippet": claim.get("evidence_snippet", ""),
        "verification_status": claim.get("verification_status", "unverified"),
        "verification_basis": claim.get("verification_basis", ""),
        "supporting_evidence_ids": claim.get("supporting_evidence_ids", []),
        "supporting_domain_count": int(claim.get("supporting_domain_count", 0) or 0),
        "published_date": claim.get("published_date", "unknown"),
        "signature": claim.get("signature", ""),
        "citation_set": citation_set,
    }
    return payload


def assemble_evidence_records(
    *,
    query_id: str,
    query: str,
    provider: str,
    source_records: list[dict[str, Any]],
    citation_records: list[dict[str, Any]],
    claim_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble per-source EvidenceRecords for one search result.

    One record per cited source (de-duplicated by EvidenceRecord id). The
    synthesized provider answer and the query summary are NOT stored on the
    records: they live once per query in ``state["query_synthesis"]`` so
    they are never duplicated across every source of a query. A record is
    ``report_eligible`` when it carries a citable URL, so URL-only sources
    (e.g. Azure Foundry, whose answer exposes no per-source body) still
    become tier-classified citation anchors instead of being dropped.

    Args:
        query_id: Stable query id.
        query: Query text.
        provider: Search provider class label.
        source_records: Normalized source registry rows for the query.
        citation_records: Normalized provider citation rows for the query.
        claim_entries: Raw claim-support entries already linked to source
            URLs and EvidenceRecord ids.

    Returns:
        EvidenceRecord dicts keyed by query plus citation/source. Each
        record carries source metadata, full text passages, and the raw
        claims supported by that source.
    """
    source_by_id = {str(record.get("source_id", "")): record for record in source_records}
    claims_by_evidence_id: dict[str, list[dict[str, Any]]] = {}
    for claim in claim_entries:
        for evidence_id in claim.get("evidence_ids", []) or []:
            claims_by_evidence_id.setdefault(str(evidence_id), []).append(claim)

    records: list[dict[str, Any]] = []
    seen_evidence_ids: set[str] = set()

    for citation_record in citation_records:
        evidence_id = evidence_id_for_citation(query_id, citation_record)
        if evidence_id in seen_evidence_ids:
            continue
        seen_evidence_ids.add(evidence_id)
        source_id = str(citation_record.get("source_id", "") or "")
        source_record = source_by_id.get(source_id, {})
        canonical_url = normalize_url(str(citation_record.get("canonical_url", "") or ""))
        claims = claims_by_evidence_id.get(evidence_id, [])
        passages = _passages_for_record(
            evidence_id=evidence_id,
            citation_record=citation_record,
            claims=claims,
        )
        citation_set = _citation_set_for_records([citation_record])
        for citation in citation_set:
            citation["evidence_id"] = evidence_id
        records.append(
            {
                "evidence_id": evidence_id,
                "record_type": "source",
                "report_eligible": bool(canonical_url),
                "query_id": query_id,
                "query": query,
                "source_id": source_id,
                "citation_id": str(citation_record.get("citation_id", "") or ""),
                "canonical_url": canonical_url,
                "domain": source_record.get("domain") or domain_from_url(canonical_url),
                "tier": source_record.get("tier", "unknown"),
                "tier_reason": source_record.get("tier_reason", ""),
                "role_reason": source_record.get("role_reason", ""),
                "provider": source_record.get("provider") or provider,
                "source_title": citation_record.get("title", ""),
                "source_snippet": citation_record.get("snippet", ""),
                "source_date": citation_record.get("source_date", "") or "unknown",
                "last_updated": citation_record.get("last_updated", "") or "unknown",
                "source_passages": passages,
                "citation_set": citation_set,
                "claims": [
                    _claim_payload(
                        claim,
                        citation_set=_claim_citation_set(
                            claim,
                            [citation_record],
                            evidence_id=evidence_id,
                            allow_overview_without_snippet=True,
                        ),
                    )
                    for claim in claims
                ],
            }
        )
    return records


def merge_evidence_records(
    existing: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Append EvidenceRecords by id without duplicating existing rows."""
    by_id: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for record in existing + new_records:
        evidence_id = str(record.get("evidence_id", "") or "")
        if evidence_id:
            by_id[evidence_id] = record
    return list(by_id.values())


def derive_claim_ledger_from_evidence(
    evidence_ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive the raw claim ledger from EvidenceRecord claims."""
    by_raw_id: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for record in evidence_ledger:
        evidence_id = str(record.get("evidence_id", "") or "")
        if not evidence_id:
            continue
        for claim in record.get("claims", []) or []:
            raw_claim_id = str(claim.get("raw_claim_id", "") or "")
            if not raw_claim_id:
                raw_claim_id = make_record_id(
                    "raw_claim",
                    record.get("query_id", ""),
                    evidence_id,
                    claim.get("signature") or claim.get("claim_text", ""),
                )
            entry = by_raw_id.setdefault(
                raw_claim_id,
                {
                    "raw_claim_id": raw_claim_id,
                    "claim_text": claim.get("claim_text", ""),
                    "evidence_snippet": claim.get("evidence_snippet", ""),
                    "claim_type": claim.get("claim_type", "fact"),
                    "polarity": claim.get("polarity", "affirmed"),
                    "needs_primary": bool(claim.get("needs_primary", False)),
                    "source_urls": [],
                    "source_ids": [],
                    "citation_ids": [],
                    "evidence_ids": [],
                    "published_date": claim.get("published_date", "unknown"),
                    "signature": claim.get("signature", ""),
                    "round": int(record.get("round", 0) or 0),
                    "query": record.get("query", ""),
                    "query_id": record.get("query_id", ""),
                },
            )
            entry["round"] = int(claim.get("round", entry.get("round", 0)) or 0)
            claim_citations = claim.get("citation_set", []) or []
            # The record's pinned tier travels with every URL it vouches for:
            # the consolidator judges a primary basis from the ledger. So does
            # the source's own text when the provider returned it (a page
            # snippet, never the provider's answer): the only text a claim
            # can be checked against.
            record_tier = str(record.get("tier", "unknown") or "unknown")
            record_tier_reason = str(record.get("tier_reason", "") or "")
            record_text = str(record.get("source_snippet", "") or "")
            source_tiers = entry.setdefault("source_tiers", {})
            source_tier_reasons = entry.setdefault("source_tier_reasons", {})
            source_texts = entry.setdefault("source_texts", {})
            if claim_citations:
                for citation in claim_citations:
                    citation_url = normalize_url(str(citation.get("url", "")))
                    _append_unique(entry["source_urls"], citation_url)
                    if citation_url:
                        source_tiers.setdefault(citation_url, record_tier)
                        source_tier_reasons.setdefault(citation_url, record_tier_reason)
                        if record_text and not source_texts.get(citation_url):
                            source_texts[citation_url] = record_text
                    _append_unique(entry["source_ids"], str(citation.get("source_id", "")))
                    _append_unique(entry["citation_ids"], str(citation.get("citation_id", "")))
            else:
                canonical = str(record.get("canonical_url", ""))
                _append_unique(entry["source_urls"], canonical)
                if canonical:
                    source_tiers.setdefault(canonical, record_tier)
                    source_tier_reasons.setdefault(canonical, record_tier_reason)
                    if record_text and not source_texts.get(canonical):
                        source_texts[canonical] = record_text
                _append_unique(entry["source_ids"], str(record.get("source_id", "")))
                _append_unique(entry["citation_ids"], str(record.get("citation_id", "")))
            _append_unique(entry["evidence_ids"], evidence_id)
            if claim_citations:
                entry.setdefault("citation_set", [])
                for citation in claim_citations:
                    if citation not in entry["citation_set"]:
                        entry["citation_set"].append(citation)
    return list(by_raw_id.values())


def project_claim_verification_to_evidence(
    evidence_ledger: list[dict[str, Any]],
    consolidated_claims: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project consolidated claim verification back onto EvidenceRecords.

    ``checked_here`` says whether THIS record's own source text was checked
    and states the claim: a claim verified through another page is still
    only attributed to this one.
    """
    by_raw_id: dict[str, dict[str, Any]] = {}
    for claim in consolidated_claims:
        for raw_claim_id in claim.get("member_claim_ids", []) or []:
            by_raw_id[str(raw_claim_id)] = claim

    for record in evidence_ledger:
        for claim in record.get("claims", []) or []:
            consolidated = by_raw_id.get(str(claim.get("raw_claim_id", "")))
            if not consolidated:
                continue
            claim["claim_id"] = consolidated.get("claim_id", "")
            claim["verification_status"] = consolidated.get("status", "unverified")
            claim["verification_basis"] = (
                consolidated.get("verification_basis")
                or consolidated.get("status_reason", "")
            )
            claim["supporting_evidence_ids"] = consolidated.get("supporting_evidence_ids", [])
            claim["supporting_domain_count"] = consolidated.get("supporting_domain_count", 0)
            claim["contradicting_evidence_ids"] = consolidated.get("contradicting_evidence_ids", [])
            claim["checked_here"] = normalize_url(
                str(record.get("canonical_url", "") or "")
            ) in set(consolidated.get("checked_urls", []) or [])
    return evidence_ledger


# --------------------------------------------------------------------------- #
# Single canonical evidence view for the final answer composer
# --------------------------------------------------------------------------- #
#
# The EvidenceLedger is the only persisted evidence truth. The final answer
# prompt consumes exactly one derived view: a record-driven Markdown overview
# rendered here. There is no separate report-evidence-bundle, prompt-evidence-
# unit, or rendered-context channel -- verification standing is read directly
# from the projected claim fields on each record.

_VERIFICATION_RANK: dict[str, int] = {
    "cross-checked": 50,
    "primary-source": 42,
    "contested": 30,
    "single-source verified": 24,
    "source-context": 12,
    "unverified": 8,
}


@dataclass(slots=True)
class EvidenceOverview:
    """Rendered single-view evidence overview for the answer composer.

    Attributes:
        markdown: The full source-indexed Markdown block handed to the
            answer prompt. Empty when no report-eligible record exists.
        label_urls: Mapping from visible per-source citation labels
            (``E1`` ..) to their canonical URLs, used to expand and validate
            answer citations without a separate citation list.
        allowed_urls: Ordered unique URLs of visible source blocks -- the
            citation allowlist for the final answer.
        label_by_evidence_id: Stable EvidenceRecord-id to citation-label
            mapping. Multiple EvidenceRecords can intentionally share a
            label when they represent the same canonical URL.
        rendered_record_count: Number of EvidenceRecords that made it
            into ``markdown``.
        omitted_record_count: Report-eligible records dropped only because
            of the character budget. Surfaced so budget loss is visible
            instead of silent.
        rendered_evidence_ids: EvidenceRecord ids whose source block is
            visible in ``markdown``.
        omitted_records: The dropped records, named: ``evidence_id``,
            ``label``, ``url``, ``query_id`` and ``reason``
            (``whole_group`` when a query's block did not fit at all,
            ``budget_tail`` when only its lower-ranked sources fell off, each
            with every place it is cited at).
            A count says that evidence was lost; this list says which,
            so a false negative in the answer can be traced to the cut.
        omitted_query_ids: Query ids that lost at least one record, in
            rank order.
        omitted_by_query: One entry per search that lost records
            (``query_id``, ``query`` text, ``omitted``, ``rendered``), in
            rank order — the report's completeness note and the parent
            run's coverage line are written from it.
    """

    markdown: str
    label_urls: dict[str, str] = field(default_factory=dict)
    allowed_urls: list[str] = field(default_factory=list)
    label_by_evidence_id: dict[str, str] = field(default_factory=dict)
    rendered_record_count: int = 0
    omitted_record_count: int = 0
    rendered_evidence_ids: list[str] = field(default_factory=list)
    omitted_records: list[dict[str, str]] = field(default_factory=list)
    omitted_query_ids: list[str] = field(default_factory=list)
    omitted_by_query: list[dict[str, Any]] = field(default_factory=list)


def _normalized_text_key(text: Any) -> str:
    """Return a whitespace-normalized lowercase key for dedup comparisons."""
    return " ".join(str(text or "").split()).lower()


def _record_primary_url(record: dict[str, Any]) -> str:
    """Return the canonical URL a record is cited by, or its first citation URL."""
    url = normalize_url(str(record.get("canonical_url", "") or ""))
    if url:
        return url
    for citation in record.get("citation_set", []) or []:
        if not isinstance(citation, dict):
            continue
        citation_url = normalize_url(str(citation.get("url", "") or ""))
        if citation_url:
            return citation_url
    return ""


def _evidence_label_number(label: str) -> int | None:
    """Return the numeric part of an ``E#`` label, or ``None``."""
    match = _EVIDENCE_LABEL_RE.fullmatch(str(label or ""))
    if not match:
        return None
    return int(match.group(1))


def _next_evidence_label(used_labels: set[str]) -> str:
    """Return the next unused compact evidence label."""
    index = 1
    while f"E{index}" in used_labels:
        index += 1
    return f"E{index}"


def _assign_url_canonical_labels(
    eligible: list[dict[str, Any]],
    *,
    existing_label_by_evidence_id: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Assign one report label per canonical URL and project it to records.

    EvidenceRecords remain query/citation-specific ledger rows, but the
    report-facing citation label represents the source URL. Reusing existing
    ``E#`` labels keeps labels stable across repeated render calls while
    collapsing any older per-record duplicates to the URL's first label.
    """
    existing = dict(existing_label_by_evidence_id or {})
    sorted_records = sorted(eligible, key=_evidence_record_score, reverse=True)
    label_by_url: dict[str, str] = {}
    label_by_evidence_id: dict[str, str] = {}
    used_labels: set[str] = set()

    for record in sorted_records:
        evidence_id = str(record.get("evidence_id", "") or "")
        url = _record_primary_url(record)
        label = existing.get(evidence_id)
        if not url or _evidence_label_number(str(label or "")) is None:
            continue
        if url not in label_by_url and str(label) not in used_labels:
            label_by_url[url] = str(label)
            used_labels.add(str(label))
        if url in label_by_url and evidence_id:
            label_by_evidence_id[evidence_id] = label_by_url[url]

    for record in sorted_records:
        evidence_id = str(record.get("evidence_id", "") or "")
        url = _record_primary_url(record)
        if not url:
            continue
        label = label_by_url.get(url)
        if not label:
            label = _next_evidence_label(used_labels)
            label_by_url[url] = label
            used_labels.add(label)
        if evidence_id:
            label_by_evidence_id[evidence_id] = label

    return label_by_evidence_id, label_by_url


def _record_support_standing(record: dict[str, Any]) -> str:
    """Return the strongest support standing across a record's claims.

    Reads the verification fields projected onto each claim by
    :func:`project_claim_verification_to_evidence`, as if every supported
    claim had been checked: ranking, section selection and the URL audit
    read this structure, so they treat every search provider alike whether
    or not it returns source text. Claimless records are
    ``source-context``: usable, single-source-attributed background.
    """
    claims = record.get("claims", []) or []
    if not claims:
        return "source-context"
    bases = {str(claim.get("verification_basis", "") or "") for claim in claims}
    statuses = {str(claim.get("verification_status", "") or "") for claim in claims}
    if "cross_checked" in bases:
        return "cross-checked"
    if "contested" in statuses or "contested" in bases:
        return "contested"
    if statuses & set(SUPPORTED_STATUSES):
        if "primary_source" in bases:
            return "primary-source"
        return "single-source verified"
    return "unverified"


def _record_verification_label(record: dict[str, Any]) -> str:
    """Return the standing a reader sees for one record.

    The support standing when this record's own source text was checked for
    one of its claims (``checked_here``); ``provider-attributed`` when its
    supported claims rest only on the search provider's answer attributing
    them to this source. ``contested``, ``unverified`` and
    ``source-context`` claim no check and stay as they are.
    """
    standing = _record_support_standing(record)
    if standing not in {"cross-checked", "primary-source", "single-source verified"}:
        return standing
    if any(claim.get("checked_here") for claim in record.get("claims", []) or []):
        return standing
    return "provider-attributed"


# What the writer and the evaluator read for each standing. The tokens stay
# the vocabulary of ranking, the URL audit, the API and forensics; a model
# quotes a short label it reads into the report, so prompts get plain words.
STANDING_TEXT = {
    "cross-checked": "its own text checked; independently confirmed",
    "primary-source": "its own text checked; primary source",
    "single-source verified": "its own text checked; one source",
    "provider-attributed": "attributed by the search provider; page not read",
    "contested": "sources disagree",
    "unverified": "insufficiently supported",
    "source-context": "background only; no extracted statement",
}


def _is_extraction_passage(origin: Any) -> bool:
    """Whether a passage is the provider text a statement was extracted from.

    Every other passage is text the search provider returned from the page.
    The full and the compact view file passages by this one rule.
    """
    return origin == "claim_evidence_snippet"


def _evidence_record_score(record: dict[str, Any]) -> int:
    """Rank evidence records for budget-limited rendering: trust then density."""
    tier = str(record.get("tier", "unknown") or "unknown")
    score = _SOURCE_CONTEXT_TIER_RANK.get(tier, _SOURCE_CONTEXT_TIER_RANK["unknown"])
    score += _VERIFICATION_RANK.get(_record_support_standing(record), 0)
    score += min(30, len(record.get("claims", []) or []) * 5)
    score += min(12, len(record.get("source_passages", []) or []) * 2)
    score += min(4, len(str(record.get("source_snippet", "") or "")) // 120)
    if str(record.get("source_date", "") or "").strip() not in {"", "unknown"}:
        score += 3
    return score


COMPACT_CLAIM_LINE_CHARS = 200
"""Per-claim line bound of the compact record view (breadth over depth)."""

COMPACT_CLAIM_LINES = 4
"""How many claim lines a source keeps in the compact record view."""

COMPACT_SYNTHESIS_CHARS = 600
"""Bound of the provider synthesis shown per search in the compact view: the
evaluator judges coverage from the per-source lines, the synthesis is
context."""


def evidence_cut_words(lost: int, searches: int, *, language: str | None) -> str:
    """The one reader phrase for an evidence-budget cut, shared by the
    report note, the mission's task note, the kernel's child report and
    the evaluator's overview note: ``"21 belegfähige Quellen aus 3 Suchen"``
    / ``"21 citable sources from 3 searches"`` (German for ``"de"``,
    English otherwise)."""
    if (language or "").lower() == "de":
        noun = "belegfähige Quelle" if lost == 1 else "belegfähige Quellen"
        if searches == 1:
            return f"{lost} {noun} aus einer Suche"
        if searches > 1:
            return f"{lost} {noun} aus {searches} Suchen"
        return f"{lost} {noun}"
    noun = "citable source" if lost == 1 else "citable sources"
    if searches == 1:
        return f"{lost} {noun} from one search"
    if searches > 1:
        return f"{lost} {noun} from {searches} searches"
    return f"{lost} {noun}"


def _tier_with_role(record: dict[str, Any], tier: str) -> str:
    """``primary (official_primary)`` when a role was judged or proven, else
    the bare tier: the writer sees who speaks, not only a rank."""
    kind, role = placement_kind(str(record.get("tier_reason", "") or ""))
    if kind in ("model", "structural") and role:
        return f"{tier} ({role})"
    return tier


def pinned_tiers_from_records(source_records: Mapping[str, Any]) -> dict[str, str]:
    """URL -> pinned tier of the run's source registry (normalized keys).

    The one projection of the registry every consumer scores against: the
    search node's source quality, the public result's top sources and
    references. A record without a URL or tier contributes nothing.
    """
    tiers: dict[str, str] = {}
    for record in source_records.values():
        if not isinstance(record, dict):
            continue
        url = normalize_url(str(record.get("canonical_url", "") or record.get("url", "") or ""))
        tier = str(record.get("tier", "") or "")
        if url and tier:
            tiers[url] = tier
    return tiers


def _provider_texts(records: list[dict[str, Any]]) -> Iterator[tuple[Any, Any]]:
    """The text the search provider returned for a source's records, each
    with the origin that files it: per record its snippet, then its
    passages."""
    for record in records:
        yield "source_snippet", record.get("source_snippet", "")
        for passage in record.get("source_passages", []) or []:
            yield passage.get("origin"), passage.get("text", "")


def _render_source_record_block(
    records: list[dict[str, Any]],
    label: str,
    *,
    max_chars: int,
    seen_texts: set[str],
    compact: bool = False,
) -> str:
    """Render one source's EvidenceRecords from one search as one labelled block.

    A search answer that cites a source at several places yields one
    EvidenceRecord per place; the writer reads the source once. The header
    comes from the best record (``records[0]``), the standing from the
    statements of all of them -- as when a search had one record per
    source -- and the content is the union of their lines.

    ``seen_texts`` is shared across the whole query group so a claim,
    snippet, passage, or summary line is never rendered twice -- this is
    what keeps per-source content substantial instead of repetitive.

    ``compact`` keeps the header (title, date, tier, verification) and
    the claim lines only — no snippets, no passages — so a view of EVERY
    source fits where the full blocks would not (the evaluator judges
    breadth; the writer reads depth).
    """
    record = records[0]
    claims = [claim for item in records for claim in item.get("claims", []) or []]
    url = _record_primary_url(record)
    title = _bounded_text(record.get("source_title", ""), 200) or url or "Untitled source"
    date = str(record.get("source_date", "") or "unknown").strip() or "unknown"
    tier = str(record.get("tier", "unknown") or "unknown")
    verification = _record_verification_label({"claims": claims})

    # No URL line: the LLM cites the bare [E#] label and the URL is
    # re-attached after synthesis (token saving, fewer citation mistakes).
    header_lines = [f"[{label}] {title}"]
    header_lines.append(
        f"  Date: {date} | Tier: {_tier_with_role(record, tier)} | Standing: {STANDING_TEXT[verification]}"
    )

    # A statement is listed as checked only when THIS source's own text was
    # checked and states it; every other statement is what the search
    # provider attributes to the source. Text the provider returned from the
    # page and the excerpts the statements were extracted from (the
    # provider's answer, for most providers) stay apart for the same reason.
    checked_lines: list[str] = []
    attributed_lines: list[str] = []
    page_lines: list[str] = []
    extraction_lines: list[str] = []
    for claim in claims:
        claim_text = _full_text(claim.get("claim_text", ""))
        if claim_text and _normalized_text_key(claim_text) not in seen_texts:
            (checked_lines if claim.get("checked_here") else attributed_lines).append(claim_text)
            seen_texts.add(_normalized_text_key(claim_text))
        if compact:
            continue
        evidence_snippet = _full_text(claim.get("evidence_snippet", ""))
        if evidence_snippet and _normalized_text_key(evidence_snippet) not in seen_texts:
            extraction_lines.append(evidence_snippet)
            seen_texts.add(_normalized_text_key(evidence_snippet))

    if compact:
        # Claims only, bounded; a claimless source keeps ONE bounded line of
        # its snippet or first passage so the evaluator can still read what
        # the source says. Snippets never enter ``seen_texts`` here: a text
        # that is not printed must not suppress a later record's claim.
        checked_lines = [
            _bounded_text(text, COMPACT_CLAIM_LINE_CHARS)
            for text in checked_lines[:COMPACT_CLAIM_LINES]
        ]
        attributed_lines = [
            _bounded_text(text, COMPACT_CLAIM_LINE_CHARS)
            for text in attributed_lines[:COMPACT_CLAIM_LINES - len(checked_lines)]
        ]
        if not checked_lines and not attributed_lines:
            for origin, text in _provider_texts(records):
                candidate = _full_text(text)
                if candidate:
                    (
                        extraction_lines if _is_extraction_passage(origin) else page_lines
                    ).append(_bounded_text(candidate, COMPACT_CLAIM_LINE_CHARS))
                    break
    else:
        for origin, text in _provider_texts(records):
            passage_text = _full_text(text)
            if passage_text and _normalized_text_key(passage_text) not in seen_texts:
                (
                    extraction_lines if _is_extraction_passage(origin) else page_lines
                ).append(passage_text)
                seen_texts.add(_normalized_text_key(passage_text))

    content_lines: list[str] = []
    for heading, lines in (
        ("  Statements checked against this source's text:", checked_lines),
        ("  Statements the search provider attributes to this source:", attributed_lines),
        ("  Text the search provider returned from this source:", page_lines),
        ("  Search-provider text these statements were taken from:", extraction_lines),
    ):
        if lines:
            content_lines.append(heading)
            content_lines.extend(f"  - {text}" for text in lines)
    if not content_lines:
        content_lines.append(
            "  Content: attributed to this source in the search answer above "
            "(no separate per-source excerpt available)."
        )

    header = "\n".join(header_lines)
    block = "\n".join([header, *content_lines])
    if len(block) <= max_chars:
        return block

    budget = max(0, max_chars - len(header) - 48)
    kept: list[str] = []
    running = 0
    for line in content_lines:
        if running + len(line) + 1 > budget:
            break
        kept.append(line)
        running += len(line) + 1
    kept.append("  [...further evidence from this source cut for budget]")
    return "\n".join([header, *kept])


RECORD_VIEW_FULL = "full"
RECORD_VIEW_COMPACT = "compact"


def render_evidence_ledger_overview(
    evidence_ledger: list[dict[str, Any]],
    *,
    max_total_chars: int,
    max_record_chars: int,
    label_by_evidence_id: dict[str, str] | None = None,
    query_synthesis: dict[str, dict[str, Any]] | None = None,
    record_view: str = RECORD_VIEW_FULL,
) -> EvidenceOverview:
    """Render the EvidenceLedger into the single canonical answer-prompt view.

    Record-driven: the report-eligible EvidenceRecords are grouped under
    the search query that produced them, so the provider synthesis is
    shown once instead of repeated per source, and every source of a
    search becomes one labelled block. A search answer that cites a source
    at several places has one record per place; their block carries all of
    them (:func:`_render_source_record_block`). Verification standing per
    source is read from the projected claim fields, so no separate
    evidence bundle is needed.

    Citation labels (``E1`` ..) are assigned per canonical URL, then
    projected to every EvidenceRecord for that URL. The rendered citation
    allowlist contains only source blocks that are actually visible in the
    prompt. Records that do not fit are counted in
    :attr:`EvidenceOverview.omitted_record_count` rather than dropped
    silently.

    The budget is spent round-robin over the score-ranked query groups:
    every open group gets its N-th source before any group gets its
    (N+1)-th, so a budget that is too small for everything thins every
    search evenly. A source is shown or left out with all the places it
    is cited at. Before, the highest-ranked groups took the largest
    prefix that fit and the lowest-ranked searches fell away whole — a
    search on the second half of a comparison question could vanish from
    the writer's view, and the report read as "nothing found" for it.

    Args:
        evidence_ledger: The run's EvidenceRecords (the primary truth),
            ideally after :func:`project_claim_verification_to_evidence`.
        max_total_chars: Hard budget for the whole rendered view.
        max_record_chars: Per-source-block budget before its evidence
            lines are compacted (the label and metadata are kept).
        label_by_evidence_id: Optional pre-assigned EvidenceRecord label
            map. Existing ``E#`` labels are reused, but collapsed to the
            URL's first label when several EvidenceRecords represent the
            same source.
        record_view: ``"full"`` renders claims, snippets and passages per
            source (the writer's view); ``"compact"`` renders header and
            claim lines only (the evaluator's breadth view).

    Returns:
        An :class:`EvidenceOverview` with the Markdown view, the label/URL
        map for visible source blocks, the visible citation allowlist, the
        stable label map, and rendered/omitted counts.
    """
    if record_view not in (RECORD_VIEW_FULL, RECORD_VIEW_COMPACT):
        raise ValueError(f"unknown record_view {record_view!r}")
    compact_view = record_view == RECORD_VIEW_COMPACT
    total_limit = max(0, int(max_total_chars or 0))
    record_limit = max(400, int(max_record_chars or 400))
    eligible = [
        record
        for record in evidence_ledger
        if record.get("report_eligible") and _record_primary_url(record)
    ]
    if not eligible or total_limit <= 0:
        lost_by_query: dict[str, dict[str, Any]] = {}
        for record in eligible:
            query_id = str(record.get("query_id", "") or "")
            entry = lost_by_query.setdefault(
                query_id,
                {
                    "query_id": query_id,
                    "query": _bounded_text(record.get("query", ""), 240),
                    "omitted": 0,
                    "rendered": 0,
                },
            )
            entry["omitted"] += 1
        return EvidenceOverview(
            markdown="",
            omitted_record_count=len(eligible),
            omitted_records=[
                {
                    "evidence_id": str(record.get("evidence_id", "") or ""),
                    "label": "",
                    "url": str(_record_primary_url(record) or ""),
                    "query_id": str(record.get("query_id", "") or ""),
                    "query": _bounded_text(record.get("query", ""), 240),
                    "reason": "whole_group",
                }
                for record in eligible
            ],
            omitted_query_ids=[qid for qid in lost_by_query if qid],
            omitted_by_query=list(lost_by_query.values()),
        )

    label_map, label_by_url = _assign_url_canonical_labels(
        eligible,
        existing_label_by_evidence_id=label_by_evidence_id,
    )
    all_label_urls = {label: url for url, label in label_by_url.items()}

    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for record in eligible:
        group_key = str(record.get("query_id", "") or record.get("query", "") or "")
        groups.setdefault(group_key, []).append(record)

    # Per group, one unit per source: the records of the places a search
    # answer cites it at, ranked by the best of them.
    ranked_groups: list[tuple[int, list[list[dict[str, Any]]]]] = []
    for records in groups.values():
        records_sorted = sorted(records, key=_evidence_record_score, reverse=True)
        units: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for record in records_sorted:
            units.setdefault(_record_primary_url(record), []).append(record)
        group_score = _evidence_record_score(records_sorted[0])
        ranked_groups.append((group_score, list(units.values())))
    ranked_groups.sort(key=lambda item: item[0], reverse=True)

    blocks: list[str] = []
    rendered = 0
    omitted = 0
    rendered_evidence_ids: list[str] = []
    rendered_labels: set[str] = set()
    omitted_records: list[dict[str, str]] = []
    omitted_by_query: list[dict[str, Any]] = []

    def _note_omitted(records_lost: list[dict[str, Any]], reason: str) -> None:
        if not records_lost:
            return
        query_id = str(records_lost[0].get("query_id", "") or "")
        query_text = _bounded_text(records_lost[0].get("query", ""), 240)
        for record in records_lost:
            evidence_id = str(record.get("evidence_id", "") or "")
            omitted_records.append(
                {
                    "evidence_id": evidence_id,
                    "label": str(label_map.get(evidence_id, "") or ""),
                    "url": str(_record_primary_url(record) or ""),
                    "query_id": query_id,
                    "query": query_text,
                    "reason": reason,
                }
            )

    def _render_group_block(
        *,
        units: list[list[dict[str, Any]]],
        query_number: int,
        synthesis: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], set[str]]:
        """Render one query group with exactly these sources visible."""
        records = [record for unit in units for record in unit]
        first = records[0]
        header_lines = [f"RESEARCH RESULT R{query_number}"]
        query_text = _bounded_text(first.get("query", ""), 240)
        if query_text:
            header_lines.append(f"Search query: {query_text}")

        visible_url_to_label: dict[str, str] = {}
        for record in records:
            evidence_id = str(record.get("evidence_id", "") or "")
            url = _record_primary_url(record)
            label = label_map.get(evidence_id)
            if url and label:
                visible_url_to_label.setdefault(url, label)

        rank_to_url = synthesis.get("citation_urls_by_rank", {}) or {}
        synthesis_text = _full_block(
            _remap_synthesis_citations(
                str(synthesis.get("provider_answer", "") or ""),
                visible_url_to_label,
                rank_to_url,
            )
        )
        if compact_view:
            synthesis_text = _bounded_text(synthesis_text, COMPACT_SYNTHESIS_CHARS)
        seen_texts: set[str] = set()
        if synthesis_text:
            header_lines.extend(
                [
                    "Search answer "
                    "(context; not independently verified):",
                    synthesis_text,
                ]
            )
            seen_texts.add(_normalized_text_key(synthesis_text))

        group_pieces = ["\n".join(header_lines), "Sources from this search:"]
        labels_in_group: set[str] = set()
        for unit in units:
            evidence_id = str(unit[0].get("evidence_id", "") or "")
            label = label_map.get(evidence_id) or "E?"
            group_pieces.append(
                _render_source_record_block(
                    unit,
                    label,
                    max_chars=record_limit,
                    seen_texts=seen_texts,
                    compact=compact_view,
                )
            )
            if label != "E?":
                labels_in_group.add(label)
        return "\n\n".join(group_pieces), records, labels_in_group

    def _block(group_index: int, count: int) -> tuple[str, list[dict[str, Any]], set[str]]:
        units = ranked_groups[group_index][1]
        synthesis = (query_synthesis or {}).get(
            str(units[0][0].get("query_id", "") or ""), {}
        )
        return _render_group_block(
            units=units[:count],
            query_number=group_index + 1,
            synthesis=synthesis,
        )

    group_count = len(ranked_groups)
    committed: list[tuple[str, list[dict[str, Any]], set[str]] | None] = [
        None
    ] * group_count
    # Fast path (the common case since the budgets were raised): every group
    # rendered whole fits — commit as is, no per-level re-rendering.
    full_blocks = [_block(group_index, len(ranked_groups[group_index][1])) for group_index in range(group_count)]
    full_chars = sum(len(block[0]) for block in full_blocks) + 2 * max(0, group_count - 1)
    if full_chars <= total_limit:
        committed = list(full_blocks)
    else:
        # Round-robin allocation over the ranked groups: pass N gives every
        # still-open group its N-th source (in rank order, with every place
        # it is cited at) before pass N+1 starts. A group whose grown block
        # no longer fits is closed — the records of its remaining sources
        # are the budget tail; a group that never fits at all falls whole.
        # A block is re-rendered per level because the synthesis header
        # remaps citations to the labels visible at that count; each
        # (group, level) pair is rendered exactly once.
        open_groups = [True] * group_count
        committed_chars = 0
        visible = 0
        level = 1
        while any(open_groups):
            for group_index in range(group_count):
                if not open_groups[group_index]:
                    continue
                units = ranked_groups[group_index][1]
                if len(units) < level:
                    open_groups[group_index] = False
                    continue
                candidate = _block(group_index, level)
                current = committed[group_index]
                current_chars = len(current[0]) if current is not None else 0
                visible_after = visible + (0 if current is not None else 1)
                projected = (
                    committed_chars
                    - current_chars
                    + len(candidate[0])
                    + 2 * max(0, visible_after - 1)
                )
                if projected > total_limit:
                    open_groups[group_index] = False
                    continue
                if current is None:
                    visible += 1
                committed[group_index] = candidate
                committed_chars += len(candidate[0]) - current_chars
            level += 1

    for group_index, (_, units) in enumerate(ranked_groups):
        # Visible sources are a prefix of the units, so their records are a
        # prefix of this list and the rest is what the budget left out.
        records = [record for unit in units for record in unit]
        block = committed[group_index]
        if block is None:
            omitted += len(records)
            _note_omitted(records, "whole_group")
            omitted_by_query.append(
                {
                    "query_id": str(records[0].get("query_id", "") or ""),
                    "query": _bounded_text(records[0].get("query", ""), 240),
                    "omitted": len(records),
                    "rendered": 0,
                }
            )
            continue
        block_text, committed_records, labels_in_group = block
        blocks.append(block_text)
        rendered += len(committed_records)
        lost = records[len(committed_records):]
        omitted += len(lost)
        _note_omitted(lost, "budget_tail")
        if lost:
            omitted_by_query.append(
                {
                    "query_id": str(records[0].get("query_id", "") or ""),
                    "query": _bounded_text(records[0].get("query", ""), 240),
                    "omitted": len(lost),
                    "rendered": len(committed_records),
                }
            )
        rendered_labels.update(labels_in_group)
        rendered_evidence_ids.extend(
            str(record.get("evidence_id", "") or "")
            for record in committed_records
            if str(record.get("evidence_id", "") or "")
        )

    omitted_query_ids = [
        str(entry.get("query_id") or "")
        for entry in omitted_by_query
        if entry.get("query_id")
    ]
    if omitted > 0 and blocks:
        blocks.append(
            f"NOTE: {evidence_cut_words(omitted, len(omitted_by_query), language='en')} "
            "did not fit into the evidence budget and are not contained in "
            "this overview."
        )

    visible_label_urls = {
        label: all_label_urls[label]
        for label in sorted(
            rendered_labels,
            key=lambda value: _evidence_label_number(value) or 10**9,
        )
        if label in all_label_urls
    }
    visible_allowed_urls = list(dict.fromkeys(visible_label_urls.values()))

    return EvidenceOverview(
        markdown="\n\n".join(blocks),
        label_urls=visible_label_urls,
        allowed_urls=visible_allowed_urls,
        label_by_evidence_id=label_map,
        rendered_record_count=rendered,
        omitted_record_count=omitted,
        rendered_evidence_ids=rendered_evidence_ids,
        omitted_records=omitted_records,
        omitted_query_ids=omitted_query_ids,
        omitted_by_query=omitted_by_query,
    )


def select_section_evidence_records(
    evidence_ledger: list[dict[str, Any]],
    *,
    section_key: str,
    required_aspects: list[str],
    used_labels: set[str],
    label_by_evidence_id: dict[str, str],
    max_records: int,
) -> list[dict[str, Any]]:
    """Select a small deterministic record subset relevant to one answer section.

    Uses only general keyword heuristics on the section's key (its
    language-independent identity, e.g. ``risks``; a custom plan's literal
    heading works the same way) plus the record's own verification
    standing, content density, and aspect-token overlap -- no provider- or
    query-type-specific branches. Records whose citation label was already
    used by an earlier section are de-prioritized so source coverage
    spreads across the whole report instead of clustering.
    """
    eligible = [
        record
        for record in evidence_ledger
        if record.get("report_eligible") and _record_primary_url(record)
    ]
    if not eligible or max_records <= 0:
        return []

    key_l = section_key.lower()
    aspect_tokens = {
        token
        for aspect in required_aspects
        for token in re.findall(r"\w+", str(aspect).lower())
        if len(token) >= 4
    }

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, record in enumerate(eligible):
        verification = _record_support_standing(record)
        score = _VERIFICATION_RANK.get(verification, 10)
        score += _SOURCE_CONTEXT_TIER_RANK.get(
            str(record.get("tier", "unknown") or "unknown"),
            _SOURCE_CONTEXT_TIER_RANK["unknown"],
        ) // 10

        if any(
            marker in key_l
            for marker in ("risk", "risik", "uncertain", "unsicher", "open", "offen")
        ):
            if verification in {"contested", "unverified"}:
                score += 40
            elif verification == "source-context":
                score += 12
        elif any(
            marker in key_l
            for marker in ("summary", "kurzfazit", "fazit", "conclusion", "outlook", "ausblick")
        ):
            if verification in {
                "cross-checked",
                "primary-source",
                "single-source verified",
                "contested",
            }:
                score += 16
        elif any(
            marker in key_l
            for marker in ("analys", "detail", "background", "hintergrund", "context", "kontext")
        ):
            score += min(12, 2 * len(record.get("source_passages", []) or []))

        searchable = " ".join(
            [
                str(record.get("source_title", "") or ""),
                " ".join(
                    str(claim.get("claim_text", "") or "")
                    for claim in record.get("claims", []) or []
                ),
            ]
        ).lower()
        if aspect_tokens and any(token in searchable for token in aspect_tokens):
            score += 8

        label = label_by_evidence_id.get(str(record.get("evidence_id", "") or ""))
        if label and label in used_labels:
            score -= 16

        scored.append((score, -index, record))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [record for _, _, record in scored[:max_records]]



def audit_answer_evidence_bindings(
    answer_body: str,
    evidence_ledger: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Audit the answer's cited sources against the EvidenceLedger.

    For every Markdown link in the answer body, resolve the URL to an
    EvidenceRecord and record that source's verification standing. There
    is no separate bundle list: a citation is ``matched`` when it resolves
    to a record carrying a supported (verified or attributed) or contested
    claim, ``source_context``
    when it resolves to a claimless or unverified record, and
    ``unknown_citation`` when it does not resolve to any record at all.
    """
    record_by_url: dict[str, dict[str, Any]] = {}
    for record in evidence_ledger:
        primary = _record_primary_url(record)
        if primary and primary not in record_by_url:
            record_by_url[primary] = record
        for citation in record.get("citation_set", []) or []:
            if not isinstance(citation, dict):
                continue
            url = normalize_url(str(citation.get("url", "") or ""))
            if url and url not in record_by_url:
                record_by_url[url] = record

    cited_urls: list[str] = []
    for match in _MARKDOWN_LINK_RE.finditer(answer_body or ""):
        url = normalize_url(match.group(2))
        if url and url not in cited_urls:
            cited_urls.append(url)

    bindings: list[dict[str, Any]] = []
    for url in cited_urls:
        record = record_by_url.get(url)
        if record is None:
            bindings.append(
                {
                    "binding_id": make_record_id("bind_ev", url, "unknown_citation"),
                    "citation_url": url,
                    "evidence_id": "",
                    "verification": "unknown",
                    "binding_status": "unknown_citation",
                }
            )
            continue
        # The binding holds when the record carries a supported or contested
        # claim, checked or attributed alike; what was checked is the
        # reported standing.
        status = (
            "matched"
            if _record_support_standing(record)
            in {"cross-checked", "primary-source", "single-source verified", "contested"}
            else "source_context"
        )
        verification = _record_verification_label(record)
        bindings.append(
            {
                "binding_id": make_record_id("bind_ev", url, status),
                "citation_url": url,
                "evidence_id": str(record.get("evidence_id", "") or ""),
                "verification": verification,
                "binding_status": status,
            }
        )
    return bindings
