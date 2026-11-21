"""Pure projection of retained evidence shared by agent readers and writers.

Callers authorize and load the material. This module never reads storage or
fetches sources; it preserves the supplied origin scope and material types.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from inqtrix.evidence import attach_web_search_lineage, reference_material_kind
from inqtrix.urls import normalize_url

NO_EVIDENCE = "(no evidence)"
"""What an evidence renderer emits when it was handed nothing.

A sentinel rather than an empty string: a prompt that simply omits
its evidence block reads as "the question needs none", while this
says "there was none to give". Shared so a reader of one renderer
recognises it in the other and in the prompts that check for it.
"""


INTERNAL_SOURCE_SCHEME = "inqtrix://"
"""URL scheme of a source that lives in this server, not on the web."""

UNTITLED_INTERNAL_SOURCE = "untitled document"
"""What an evidence card calls an internal document that carries no title."""


def is_internal_reference(ref: dict[str, Any]) -> bool:
    """Whether a reference points into this server rather than to the web.

    The same test the ledger labels by (``K`` for a document id, ``W``
    otherwise), widened by the internal URL scheme so a reference that
    reaches a renderer without its document id is still recognised.
    """
    return ref.get("document_id") is not None or str(
        ref.get("url") or ""
    ).startswith(INTERNAL_SOURCE_SCHEME)


def internal_reference_name(ref: dict[str, Any]) -> str:
    """What a writer prompt calls an internal document: its title.

    An internal document has no reader-facing identity outside this server
    - its id and its ``inqtrix://`` address are bookkeeping for tool
    arguments. A card that printed either put the exact string the writer
    guard then has to keep out of the prose into the writer's context, and
    a model that copied it wrote an id no reader can resolve. A web source
    keeps its URL: there the address IS what a reader would look up.

    The cards are not the guard. An excerpt can carry an id of its own,
    and a role reads the provenance record whole
    (``kernel/evidence_read.py``); only
    :func:`~inqtrix.agents.report_quality.introduced_identifiers` decides
    what reaches the reader.
    """
    return str(ref.get("title") or "").strip() or UNTITLED_INTERNAL_SOURCE


def _ids(record: dict[str, Any], singular: str, plural: str) -> list[str]:
    values = record.get(plural) or []
    if not isinstance(values, (list, tuple)):
        values = []
    return list(dict.fromkeys(
        value for value in [record.get(singular), *values]
        if isinstance(value, str) and value
    ))


def reference_origins(references: list[dict[str, Any]]) -> list[str]:
    """Retained source origins, without guessing from the artifact writer."""
    return list(dict.fromkeys(
        run_id for ref in references
        for run_id in _ids(ref, "source_run_id", "source_run_ids")
    ))


def project_evidence_context(
    references: list[dict[str, Any]],
    ledger: dict[str, Any],
    *,
    unavailable: Sequence[dict[str, str]] = (),
) -> dict[str, Any]:
    """Project selected references and their full, deduplicated search context.

    Reference labels belong to their output; they are not global lookup keys.
    An existing source excerpt is retained verbatim. Provider answer context
    is separately named and never promoted to an original webpage passage.
    """
    blocked = {item["source_run_id"] for item in unavailable}
    searches = ledger.get("searches") or {}
    usable = {
        str(key): row for key, row in searches.items()
        if isinstance(row, dict) and row.get("source_run_id") not in blocked
    }
    selected: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []
    for original in references:
        origins = set(_ids(original, "source_run_id", "source_run_ids"))
        queries = set(_ids(original, "query_id", "query_ids"))
        # A matching URL cannot widen a reference's authorized origin scope.
        # Legacy references without origin ids retain their excerpt/metadata;
        # callers can select a known query explicitly to authorize a full read.
        scoped = {
            key: row for key, row in usable.items()
            if row.get("source_run_id") in origins and (not queries or key in queries)
        }
        ref = attach_web_search_lineage([original], {"searches": scoped})[0]
        query_ids = _ids(ref, "query_id", "query_ids")
        source_ids = set(_ids(ref, "source_run_id", "source_run_ids"))
        url = normalize_url(str(ref.get("url") or ""))
        matches = []
        for query_id in query_ids:
            row = scoped.get(query_id)
            if row is None or (
                source_ids and row.get("source_run_id") not in source_ids
            ):
                continue
            citations = [
                item for item in (row.get("citations") or [])
                if isinstance(item, dict)
                and normalize_url(str(item.get("url") or "")) == url
            ]
            if not citations:
                continue
            selected[query_id] = row
            matches.extend({"query_id": query_id, **item} for item in citations)
        original = str(ref.get("excerpt") or ref.get("source_text") or "")
        sources.append({
            # The statements a research report ties to a reference are a
            # reading aid of that report; an agent reads the stored texts
            # themselves, and the list would only repeat them in its prompt.
            "reference": {key: value for key, value in ref.items() if key != "statements"},
            "material_kind": reference_material_kind(ref, search_context=bool(matches)),
            "source_excerpt": original,
            "provider_context": str(ref.get("grounded_support") or ""),
            "citation_contexts": matches,
            "search_context_status": "available" if matches else "unavailable",
        })
    return deepcopy({
        "sources": sources,
        "searches": selected,
        "unavailable_origins": list(unavailable),
    })


