"""Knowledge capabilities (read-only).

Thin wrappers over :class:`~inqtrix.services.knowledge_service.KnowledgeService`.
The agent path uses STRICT collection visibility (``knowledge.search``
asserts every requested collection is visible, unlike the legacy debug
route that silently filters) — an agent must never plan against fewer
sources than it asked for (No Silent Fallbacks).
"""

from __future__ import annotations

from typing import Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from inqtrix.agents.context_sources import (
    MaterialReadInput,
    MaterialReadOutput,
    material_version,
)
from inqtrix.capabilities.catalog.context import read_material_window

from inqtrix.capabilities.contracts import (
    CapabilityContext,
    CapabilityDefinition,
    CapabilityError,
    Effect,
)
from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentChunk,
    DocumentNotFound,
    VectorStoreUnavailable,
)
from inqtrix.knowledge.evidence import (
    KnowledgeEvidenceProjector,
    UnverifiedKnowledgeEvidence,
)
from inqtrix.knowledge.grounding import quote_is_verbatim
from inqtrix.knowledge.retrieval_warnings import (
    project_retrieval_exclusion_warnings,
    warning_message,
)
from inqtrix.quote_limits import CITE_QUOTE_MIN_WORDS, quote_word_count
from inqtrix.services.knowledge_service import KnowledgeValidationError

if TYPE_CHECKING:
    from inqtrix.services.knowledge_service import KnowledgeService

_TOP_K_MAX = 50

# No standalone knowledge.chunk.read capability: a search hit already carries
# its verified excerpt, stable citation coordinates, and page provenance.


class CollectionsListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectionSummary(BaseModel):
    id: str
    name: str
    embedding_model: str
    document_count: int


class CollectionsListOutput(BaseModel):
    collections: list[CollectionSummary]


class KnowledgeSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1)
    collection_ids: list[str] = Field(default_factory=list)
    top_k: int = Field(8, ge=1, le=_TOP_K_MAX)


class KnowledgeHit(BaseModel):
    document_id: str
    collection_id: str
    document_title: str
    chunk_index: int
    chunk_id: str
    rank: int
    excerpt: str
    page_number: int | None
    score: float
    source_span: dict[str, object] | None = None
    revision_id: str | None = None
    generation_id: str | None = None
    provenance_status: str


class KnowledgeSearchWarning(BaseModel):
    code: str
    message: str
    retrieval_mode: str = ""
    stage: str = ""
    requested_candidate_pool: int = Field(0, ge=0)
    returned_candidate_pool: int = Field(0, ge=0)
    final_top_k: int = Field(0, ge=0)
    final_evidence_complete: bool = False
    # Compatibility projection for clients predating the explicit candidate
    # pool fields. These counters now always describe the final evidence set.
    requested_top_k: int = Field(0, ge=0)
    returned_hits: int = Field(0, ge=0)
    candidate_cap: int | None = Field(None, ge=0)
    count: int = Field(0, ge=0)


class KnowledgeSearchOutput(BaseModel):
    query: str
    hits: list[KnowledgeHit]
    warnings: list[KnowledgeSearchWarning] = Field(default_factory=list)


class DocumentReadInput(MaterialReadInput):
    document_id: str = Field(..., min_length=1)


class DocumentReadOutput(MaterialReadOutput):
    id: str
    collection_id: str
    title: str
    text: str
    chunk_count: int
    # Parser identity and extraction coverage of a file-derived document,
    # recorded by the server at ingestion; None when no facts exist.
    extraction: dict[str, Any] | None = None
    # Whether the document came from a file and, if so, whether its
    # extraction facts were recorded. ``not_recorded`` is a file-derived
    # document ingested before the facts existed: its missing pages cannot
    # be named, and a reader must say that instead of saying nothing.
    extraction_status: Literal["recorded", "not_recorded", "none"] = "none"


CITE_QUOTES_MAX = 250
"""Quotes one citation call carries at most (a whole document's findings)."""

CITE_QUOTE_CHARS_MAX = 1_200
"""Longest quote the citation accepts; longer text is a passage, not a quote."""

CitationStatus = Literal[
    "cited", "too_short", "not_in_window", "spans_chunks", "chunk_unverified", "chunk_stale",
]


class QuoteToCite(BaseModel):
    """One quote with the window ``[start, end)`` its reader saw it in."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=CITE_QUOTE_CHARS_MAX)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(ge=0)


class DocumentCiteInput(BaseModel):
    """Quotes a reader took from windows of one pinned document version."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(..., min_length=1)
    version: str = Field(..., min_length=1)
    quotes: list[QuoteToCite] = Field(default_factory=list, max_length=CITE_QUOTES_MAX)


class QuoteCitation(BaseModel):
    index: int
    status: CitationStatus
    evidence: dict[str, Any] | None = None


class DocumentCiteOutput(BaseModel):
    document_id: str
    version: str
    citations: list[QuoteCitation]


def _cite_quote(
    quote: QuoteToCite,
    *,
    text: str,
    content_hash: str,
    chunks: list[DocumentChunk],
    title: str,
    byte_at: Any,
) -> tuple[CitationStatus, dict[str, Any] | None]:
    """Where one quote stands, and its canonical chunk evidence when citable.

    The quote must be verbatim (THE normalizer) in the window its reader
    actually saw — not anywhere in the document — and one stored chunk
    overlapping that window must contain it, belong to the pinned document
    content and pass the evidence projector. Chunk spans are UTF-8 byte
    offsets, windows are code-point offsets: ``byte_at`` converts.
    """
    if quote_word_count(quote.text) < CITE_QUOTE_MIN_WORDS:
        return "too_short", None
    start, end = quote.start, quote.end
    if not (0 <= start < end <= len(text)) or not quote_is_verbatim(
        quote.text, [text[start:end]]
    ):
        return "not_in_window", None
    first, last = byte_at(start), byte_at(end)
    overlapping = [
        chunk
        for chunk in chunks
        if chunk.source_start is not None
        and chunk.source_end is not None
        and chunk.source_start < last
        and chunk.source_end > first
    ]
    if not overlapping:
        return "chunk_unverified", None
    holders = [
        chunk
        for chunk in overlapping
        if chunk.source_text and quote_is_verbatim(quote.text, [chunk.source_text])
    ]
    if not holders:
        return "spans_chunks", None
    current = [chunk for chunk in holders if chunk.document_content_hash == content_hash]
    if not current:
        return "chunk_stale", None
    try:
        evidence = KnowledgeEvidenceProjector.project_chunk(
            current[0],
            reference_id=f"K{current[0].chunk_index + 1}",
            title=title,
        ).as_dict()
    except UnverifiedKnowledgeEvidence:
        return "chunk_unverified", None
    # Like a search hit, a citation carries no reference label: the caller's
    # evidence ledger assigns labels, and a label-shaped id here would read
    # as an internal id wherever the visible label is cited.
    evidence.pop("reference_id", None)
    return "cited", evidence


_FILE_ORIGIN_KEYS = ("file_id", "fileId", "source_file_sha256", "source_parser_id")
"""Metadata the file ingestion writes next to the extraction facts
(``routers/knowledge.py``); their presence without facts marks a file-derived
document whose extraction was never recorded."""


def _extraction_of(
    metadata: dict[str, Any],
) -> tuple[dict[str, Any] | None, Literal["recorded", "not_recorded", "none"]]:
    facts = metadata.get("extraction")
    if isinstance(facts, dict) and facts.get("parser_id"):
        return dict(facts), "recorded"
    if any(metadata.get(key) for key in _FILE_ORIGIN_KEYS):
        return None, "not_recorded"
    return None, "none"


def build_knowledge_capabilities(
    service: "KnowledgeService",
) -> list[CapabilityDefinition]:
    """Build the read-only knowledge capabilities bound to *service*."""

    async def _list(
        _payload: CollectionsListInput, context: CapabilityContext
    ) -> CollectionsListOutput:
        collections = await service.list_collections(
            visible_to=context.visible_to,
        )
        if context.knowledge_collection_ids is not None:
            collections = [
                collection
                for collection in collections
                if collection.id in context.knowledge_collection_ids
            ]
        return CollectionsListOutput(
            collections=[
                CollectionSummary(
                    id=collection.id,
                    name=collection.name,
                    embedding_model=collection.embedding_model,
                    document_count=collection.document_count,
                )
                for collection in collections
            ]
        )

    async def _search(
        payload: KnowledgeSearchInput, context: CapabilityContext
    ) -> KnowledgeSearchOutput:
        collection_ids = list(payload.collection_ids)
        pinned_ids = context.knowledge_collection_ids
        if pinned_ids is not None:
            if collection_ids and not set(collection_ids).issubset(pinned_ids):
                raise CapabilityError(
                    "knowledge.collection_not_found",
                    "At least one requested collection is not visible.",
                    http_status=404,
                )
            if not collection_ids:
                collection_ids = sorted(pinned_ids)
            if not collection_ids:
                # An EMPTY boundary is not "nothing matched" — it
                # is "there is nothing to search". Returning bare empty
                # hits made the two indistinguishable for the model and
                # for the user; the reason travels as a warning so no
                # downstream layer has to guess it.
                return KnowledgeSearchOutput(
                    query=payload.query,
                    hits=[],
                    warnings=[
                        KnowledgeSearchWarning(
                            code="knowledge.no_collections",
                            message=warning_message("knowledge.no_collections"),
                            stage="scope",
                        )
                    ],
                )

        # The searched boundary rides the capability span (a plain child of
        # the tool observation) as plain attributes: without them a trace
        # cannot say which collections a run searched, and the query itself
        # is content-policy gated. An unscoped search (no pinned boundary,
        # no ids) is named as such rather than as a zero-collection search.
        from inqtrix.observability import semconv
        from inqtrix.observability.otel import enrich_current_span

        enrich_current_span(
            {
                semconv.INQTRIX_KNOWLEDGE_SCOPE: "pinned",
                semconv.INQTRIX_KNOWLEDGE_COLLECTION_IDS: sorted(collection_ids),
                semconv.INQTRIX_KNOWLEDGE_COLLECTION_COUNT: len(collection_ids),
            }
            if collection_ids
            else {semconv.INQTRIX_KNOWLEDGE_SCOPE: "unbounded"}
        )
        try:
            # Strict: an explicit collection set the agent cannot fully see
            # is a denial, not a quiet narrowing of the search.
            if collection_ids:
                await service.assert_collections_visible(
                    collection_ids,
                    visible_to=context.visible_to,
                )
            outcome = await service.search_reported(
                query=payload.query,
                collection_ids=collection_ids or None,
                top_k=payload.top_k,
                visible_to=context.visible_to,
            )
        except KnowledgeValidationError as exc:
            raise CapabilityError("invalid_input", str(exc), http_status=400) from exc
        except CollectionNotFound as exc:
            raise CapabilityError(
                "knowledge.collection_not_found",
                "At least one requested collection is not visible.",
                http_status=404,
            ) from exc
        except VectorStoreUnavailable as exc:
            # An outage is a loud, typed failure: the caller (kernel tool,
            # REST adapter) names it; nothing falls back to another source.
            raise CapabilityError(
                "knowledge.store_unavailable",
                "The knowledge store is unreachable; project knowledge "
                "cannot be searched in this run.",
                http_status=503,
            ) from exc
        if outcome.filtered_collection_ids:
            # Visibility may change between the admission check and retrieval.
            # The strict agent contract must still fail closed instead of
            # silently answering from the surviving subset.
            raise CapabilityError(
                "knowledge.collection_not_found",
                "At least one requested collection is not visible.",
                http_status=404,
            )
        candidates = outcome.candidates
        warnings = [
            KnowledgeSearchWarning(
                code=degradation.reason,
                message=warning_message(
                    degradation.reason,
                    final_evidence_complete=degradation.final_evidence_complete,
                ),
                retrieval_mode=degradation.retrieval_mode,
                stage=degradation.stage,
                requested_candidate_pool=(
                    degradation.requested_candidate_pool or 0
                ),
                returned_candidate_pool=(
                    degradation.returned_candidate_pool or 0
                ),
                final_top_k=degradation.final_top_k or 0,
                final_evidence_complete=(
                    degradation.final_evidence_complete
                ),
                requested_top_k=degradation.requested_top_k,
                returned_hits=degradation.returned_hits,
                candidate_cap=degradation.candidate_cap,
            )
            for degradation in outcome.retrieval_degradations
        ]
        for warning in project_retrieval_exclusion_warnings(
            outcome.retrieval_exclusions
        ):
            warnings.append(
                KnowledgeSearchWarning(
                    code=warning.code,
                    message=warning.message,
                    requested_top_k=payload.top_k,
                    returned_hits=len(candidates),
                    count=warning.count,
                )
            )
        return KnowledgeSearchOutput(
            query=payload.query,
            hits=[
                KnowledgeHit(
                    document_id=evidence.document_id,
                    collection_id=evidence.collection_id,
                    document_title=evidence.title,
                    chunk_index=evidence.chunk_index,
                    chunk_id=evidence.chunk_id,
                    rank=rank,
                    excerpt=evidence.excerpt,
                    page_number=evidence.page_number,
                    score=round(evidence.score, 6),
                    source_span=evidence.as_dict()["source_span"],
                    revision_id=evidence.revision_id,
                    generation_id=evidence.generation_id,
                    provenance_status=evidence.provenance_status,
                )
                for rank, candidate in enumerate(candidates, start=1)
                for evidence in (
                    KnowledgeEvidenceProjector.project(
                        candidate, reference_id=f"K{rank}"
                    ),
                )
            ],
            warnings=warnings,
        )

    async def _readable_document(document_id: str, context: CapabilityContext):
        """The ONE access path of every document-level knowledge capability.

        Visibility for the principal and the run's pinned collection
        boundary; an unknown, invisible or out-of-boundary document is the
        same indistinct 404.
        """
        try:
            document = await service.get_document(
                document_id,
                visible_to=context.visible_to,
            )
        except DocumentNotFound as exc:
            raise CapabilityError(
                "knowledge.document_not_found",
                "Document not found.",
                http_status=404,
            ) from exc
        if (
            context.knowledge_collection_ids is not None
            and document.collection_id not in context.knowledge_collection_ids
        ):
            raise CapabilityError(
                "knowledge.document_not_found",
                "Document not found.",
                http_status=404,
            )
        return document

    async def _read(
        payload: DocumentReadInput, context: CapabilityContext
    ) -> DocumentReadOutput:
        document = await _readable_document(payload.document_id, context)
        text, window = read_material_window(
            document.text, document.active_revision_id or "legacy", payload,
        )
        extraction, extraction_status = _extraction_of(document.metadata)
        return DocumentReadOutput(
            id=document.id,
            collection_id=document.collection_id,
            title=document.title,
            text=text,
            chunk_count=document.chunk_count,
            extraction=extraction,
            extraction_status=extraction_status,
            window=window,
            representation="knowledge_text",
        )

    async def _cite(
        payload: DocumentCiteInput, context: CapabilityContext
    ) -> DocumentCiteOutput:
        document = await _readable_document(payload.document_id, context)
        text = document.text
        version, content_hash = material_version(
            text, document.active_revision_id or "legacy"
        )
        if payload.version != version:
            raise CapabilityError(
                "context.version_changed",
                "Document changed; restart the read against its current version.",
                http_status=409,
            )
        try:
            chunks = sorted(
                await service.document_chunks(
                    document.id, visible_to=context.visible_to
                ),
                key=lambda chunk: chunk.chunk_index,
            )
        except DocumentNotFound as exc:
            raise CapabilityError(
                "knowledge.document_not_found",
                "Document not found.",
                http_status=404,
            ) from exc
        byte_offsets: dict[int, int] = {}

        def byte_at(position: int) -> int:
            if position not in byte_offsets:
                byte_offsets[position] = len(text[:position].encode("utf-8"))
            return byte_offsets[position]

        citations = []
        for index, quote in enumerate(payload.quotes):
            status, evidence = _cite_quote(
                quote,
                text=text,
                content_hash=content_hash,
                chunks=chunks,
                title=document.title,
                byte_at=byte_at,
            )
            citations.append(
                QuoteCitation(index=index, status=status, evidence=evidence)
            )
        return DocumentCiteOutput(
            document_id=document.id, version=version, citations=citations
        )

    return [
        CapabilityDefinition(
            id="knowledge.collections.list",
            summary="List the knowledge collections the caller can see.",
            input_model=CollectionsListInput,
            output_model=CollectionsListOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_list,
        ),
        CapabilityDefinition(
            id="knowledge.search",
            summary=(
                "Search project knowledge collections; requested collections "
                "must all be visible (strict)."
            ),
            input_model=KnowledgeSearchInput,
            output_model=KnowledgeSearchOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_search,
        ),
        CapabilityDefinition(
            id="knowledge.document.read",
            summary="Read one knowledge document's full text and provenance.",
            input_model=DocumentReadInput,
            output_model=DocumentReadOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_read,
        ),
        CapabilityDefinition(
            id="knowledge.document.cite",
            summary=(
                "Map verbatim quotes from windows of one pinned document "
                "version to verified chunk evidence."
            ),
            input_model=DocumentCiteInput,
            output_model=DocumentCiteOutput,
            effect=Effect.READ,
            idempotent=True,
            handler=_cite,
        ),
    ]
