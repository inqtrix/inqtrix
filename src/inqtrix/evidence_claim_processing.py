"""Shared claim projection helpers for Research and Agent orchestration."""

from __future__ import annotations

from typing import Any

from inqtrix.strategies._claim_extraction import (
    PROVIDER_METADATA_HEADING,
    ProviderCitationRef,
)
from inqtrix.urls import normalize_url


def claim_extraction_text(
    answer_text: str,
    citation_records: list[dict[str, Any]],
    *,
    max_sources: int = 8,
) -> str:
    """Project the provider-grounded answer and its citation metadata.

    Inqtrix does not fetch the linked pages. Provider snippets are labelled as
    such and the coherent provider answer remains the primary extraction
    input; neither is presented as a verbatim excerpt independently read from
    a website.
    """

    source_lines: list[str] = []
    seen_urls: set[str] = set()
    for record in citation_records:
        url = normalize_url(
            str(
                record.get("canonical_url", "")
                or record.get("url", "")
                or ""
            )
        )
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = " ".join(str(record.get("title", "") or "").split())[:140]
        date = " ".join(str(record.get("source_date", "") or "").split())[:80]
        rank = record.get("rank")
        prefix = f"[{rank}] {url}" if rank else f"- {url}"
        if title:
            prefix += f" | title: {title}"
        if date:
            prefix += f" | date: {date}"
        snippet = " ".join(str(record.get("snippet", "") or "").split())
        if snippet:
            prefix += f"\n  PROVIDER_SNIPPET[{rank or '-'}]: {snippet}"
        source_lines.append(prefix)
        if len(seen_urls) >= max_sources:
            break
    if not source_lines:
        return answer_text
    # The downstream extractor applies its profile-wide input budget to the
    # completed projection. Keep the coherent provider answer first so source
    # metadata or snippets cannot displace the primary search result.
    return (
        "Coherent, web-grounded provider answer:\n"
        + answer_text
        + f"\n\n{PROVIDER_METADATA_HEADING}\n"
        + "\n".join(source_lines)
    )


def claim_provider_refs(
    citation_records: list[dict[str, Any]],
) -> list[ProviderCitationRef]:
    """Return stable provider-local refs for deterministic source pinning."""

    refs: list[ProviderCitationRef] = []
    seen: set[str] = set()
    for record in citation_records:
        rank = str(record.get("rank", "") or "").strip()
        url = normalize_url(
            str(
                record.get("canonical_url", "")
                or record.get("url", "")
                or ""
            )
        )
        if not rank or not url:
            continue
        key = f"{rank}|{url}"
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            ProviderCitationRef(
                ref=rank,
                url=url,
                title=str(record.get("title", "") or ""),
            )
        )
    return refs
