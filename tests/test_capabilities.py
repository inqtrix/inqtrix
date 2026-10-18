"""Capability layer: contract, registry, and read-only catalog.

The capabilities wrap existing services with a typed envelope and an
injected :class:`CapabilityContext`. These tests pin (1) the registry
behaviour (ordered, loud on duplicates, input validation), (2) that
each capability returns the SAME data as calling its service directly
(the "one implementation, three adapters" guarantee), and (3) the
strict-collections semantics for the agent search path.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import threading
import uuid

import pytest

from inqtrix.capabilities import (
    CapabilityContext,
    CapabilityError,
    build_capability_registry,
)
from inqtrix.capabilities.contracts import (
    CapabilityDefinition,
    Effect,
)
from inqtrix.capabilities.registry import CapabilityRegistry, UnknownCapability
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService, SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    KnowledgeProviderContext,
    RetrievalDegradation,
)
from inqtrix.search_result import CitationSpan, GroundedSearchResult, GroundedSource
from inqtrix.services.knowledge_service import KnowledgeService, SearchOutcome
from pydantic import BaseModel

from tests.test_knowledge_engine import StubEmbeddings


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #


def make_knowledge_service(
    identity: MemoryIdentityStore | None = None,
) -> KnowledgeService:
    identity = identity or MemoryIdentityStore()
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=4,
        ),
        authorization=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )


class _StubSearch:
    """Search provider stub returning two grounded sources."""

    def search(self, query, **kwargs):
        self.last_kwargs = kwargs
        return GroundedSearchResult(
            answer=f"Antwort zu {query}",
            sources=[
                GroundedSource(url="https://a.example", title="A", snippet="s1", rank=1),
                GroundedSource(url="https://b.example", title="B", snippet="s2", rank=2),
            ],
            prompt_tokens=11,
            completion_tokens=7,
        )

    def is_available(self) -> bool:
        return True


class _FallbackSearch(_StubSearch):
    """Provider-style fallback exposed from the same search thread."""

    def search(self, query: str, **kwargs: object) -> GroundedSearchResult:
        del query, kwargs
        self._search_thread = threading.get_ident()
        return GroundedSearchResult()

    def consume_nonfatal_notice_detail(self) -> dict[str, object]:
        assert threading.get_ident() == self._search_thread
        return {
            "code": "upstream_5xx",
            "message": "Upstream search unavailable.",
            "http_status": 503,
        }


class _ProviderTimeoutFallbackSearch(_FallbackSearch):
    def consume_nonfatal_notice_detail(self) -> dict[str, object]:
        assert threading.get_ident() == self._search_thread
        return {
            "code": "provider_timeout",
            "message": "Upstream search timed out.",
            "http_status": 504,
        }


_ANON_PRINCIPAL = Principal(user_id=None, kind="anonymous")
# Unscoped context: visible_to=None means "no membership filtering",
# exactly what an anonymous/static principal resolves to.
ANON = CapabilityContext(principal=_ANON_PRINCIPAL)


def _user_id(label: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"capability:{label}")


def scoped_context(sub: str) -> CapabilityContext:
    """A scoped (oidc-session) caller with an empty membership set —
    sees only what it owns or was granted (nothing here)."""
    principal = Principal(
        user_id=_user_id(sub),
        kind="oidc_session",
    )
    return CapabilityContext(
        principal=principal, visible_to=UserContext(principal=principal)
    )


# ------------------------------------------------------------------ #
# registry
# ------------------------------------------------------------------ #


def _dummy(effect=Effect.READ) -> CapabilityDefinition:
    class _In(BaseModel):
        x: int

    class _Out(BaseModel):
        y: int

    async def _h(payload, _ctx):
        return _Out(y=payload.x + 1)

    return CapabilityDefinition(
        id="dummy.op",
        summary="s",
        input_model=_In,
        output_model=_Out,
        effect=effect,
        idempotent=True,
        handler=_h,
    )


def test_registry_is_ordered_and_loud_on_duplicates():
    registry = CapabilityRegistry()
    registry.register(_dummy())
    assert registry.ids() == ("dummy.op",)
    with pytest.raises(ValueError):
        registry.register(_dummy())
    with pytest.raises(UnknownCapability):
        registry.get("nope")


def test_registry_validates_input_and_maps_to_capability_error():
    registry = CapabilityRegistry()
    registry.register(_dummy())
    # Missing required field -> CapabilityError(invalid_input, 400).
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(registry.invoke("dummy.op", {}, ANON))
    assert excinfo.value.code == "invalid_input"
    assert excinfo.value.http_status == 400
    # Valid input runs the handler.
    out = asyncio.run(registry.invoke("dummy.op", {"x": 41}, ANON))
    assert out.y == 42


def test_manifest_entry_mirrors_mcp_annotation_vocabulary():
    entry = _dummy(effect=Effect.READ).manifest_entry()
    assert entry["read_only"] is True
    assert entry["destructive"] is False
    assert entry["idempotent"] is True
    write = _dummy(effect=Effect.WRITE).manifest_entry()
    assert write["read_only"] is False


# ------------------------------------------------------------------ #
# catalog: knowledge (service-identity guarantee)
# ------------------------------------------------------------------ #


def test_knowledge_search_capability_matches_direct_service_call():
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)

    collection = asyncio.run(service.create_collection(name="Recht"))
    asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Haftung",
            text="Die Haftung ist auf den Auftragswert begrenzt.",
        )
    )

    # Direct service call and capability invocation return the same hits.
    direct = asyncio.run(service.search(query="Haftung Auftragswert"))
    out = asyncio.run(
        registry.invoke(
            "knowledge.search", {"query": "Haftung Auftragswert"}, ANON
        )
    )
    assert [hit.document_id for hit in out.hits] == [
        candidate.chunk.document_id for candidate in direct
    ]
    assert out.hits[0].rank == 1
    assert out.hits[0].chunk_id.startswith("kch_")
    assert out.hits[0].excerpt  # original evidence projection present


@pytest.mark.parametrize(
    ("reason", "candidate_cap"),
    [
        ("vector_overfetch_cap", 64),
        ("vector_candidate_stalled", None),
    ],
)
def test_knowledge_search_capability_exposes_retrieval_degradation(
    reason: str,
    candidate_cap: int | None,
) -> None:
    service = make_knowledge_service()
    collection = asyncio.run(service.create_collection(name="Recht"))
    asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Haftung",
            text="Die Haftung ist auf den Auftragswert begrenzt.",
        )
    )
    original = service.search_reported

    async def degraded_search(**kwargs) -> SearchOutcome:
        outcome = await original(**kwargs)
        return replace(
            outcome,
            retrieval_degradations=[
                RetrievalDegradation(
                    reason=reason,
                    retrieval_mode="dense",
                    requested_top_k=4,
                    returned_hits=len(outcome.candidates),
                    candidate_cap=candidate_cap,
                    requested_candidate_pool=40,
                    returned_candidate_pool=8,
                    final_top_k=4,
                )
            ],
        )

    service.search_reported = degraded_search  # type: ignore[method-assign]
    registry = build_capability_registry(knowledge_service=service)

    output = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {"query": "Haftung", "top_k": 4},
            ANON,
        )
    )

    assert output.hits
    assert [warning.code for warning in output.warnings] == [reason]
    assert output.warnings[0].returned_hits == len(output.hits)
    assert output.warnings[0].candidate_cap == candidate_cap
    assert output.warnings[0].stage == "vector_candidate_pool"
    assert output.warnings[0].requested_candidate_pool == 40
    assert output.warnings[0].returned_candidate_pool == 8
    assert output.warnings[0].final_top_k == 4
    assert output.warnings[0].final_evidence_complete is False
    assert "requested final evidence count" in output.warnings[0].message


@pytest.mark.parametrize(
    ("reason", "candidate_cap"),
    [
        ("vector_overfetch_cap", 64),
        ("vector_candidate_stalled", None),
    ],
)
def test_knowledge_search_warning_distinguishes_complete_final_evidence(
    reason: str,
    candidate_cap: int | None,
) -> None:
    service = make_knowledge_service()
    collection = asyncio.run(service.create_collection(name="Recht"))
    asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Haftung",
            text="Die Haftung ist auf den Auftragswert begrenzt.",
        )
    )
    original = service.search_reported

    async def candidate_pool_underfilled(**kwargs) -> SearchOutcome:
        outcome = await original(**kwargs)
        candidates = outcome.candidates * 4
        return replace(
            outcome,
            candidates=candidates,
            retrieval_degradations=[
                RetrievalDegradation(
                    reason=reason,
                    retrieval_mode="dense",
                    requested_top_k=4,
                    returned_hits=4,
                    candidate_cap=candidate_cap,
                    requested_candidate_pool=40,
                    returned_candidate_pool=8,
                    final_top_k=4,
                )
            ],
        )

    service.search_reported = candidate_pool_underfilled  # type: ignore[method-assign]
    registry = build_capability_registry(knowledge_service=service)

    output = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {"query": "Haftung", "top_k": 4},
            ANON,
        )
    )

    assert len(output.hits) == 4
    assert output.warnings[0].code == reason
    assert output.warnings[0].candidate_cap == candidate_cap
    assert output.warnings[0].final_evidence_complete is True
    assert output.warnings[0].stage == "vector_candidate_pool"
    assert output.warnings[0].requested_candidate_pool == 40
    assert output.warnings[0].returned_candidate_pool == 8
    assert output.warnings[0].final_top_k == 4
    assert output.warnings[0].requested_top_k == 4
    assert output.warnings[0].returned_hits == 4
    assert "full final evidence count was nevertheless reached" in (
        output.warnings[0].message
    )


def test_knowledge_document_read_capability_returns_full_text():
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)
    collection = asyncio.run(service.create_collection(name="Recht"))
    document = asyncio.run(
        service.add_document(
            collection_id=collection.id, title="D", text="Voller Text."
        )
    )
    out = asyncio.run(
        registry.invoke("knowledge.document.read", {"document_id": document.id}, ANON)
    )
    assert out.text == "Voller Text."
    assert out.id == document.id


def test_knowledge_document_read_unknown_is_capability_error_404():
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(
            registry.invoke(
                "knowledge.document.read", {"document_id": "kd_nope"}, ANON
            )
        )
    assert excinfo.value.http_status == 404


def test_knowledge_capabilities_enforce_pinned_run_collection_scope():
    """Live visibility may grow, but one admitted run never widens its corpus."""
    identity = MemoryIdentityStore()
    service = make_knowledge_service(identity)
    registry = build_capability_registry(knowledge_service=service)
    recipient = scoped_context("scope-recipient")
    owner = scoped_context("scope-owner")

    admitted = asyncio.run(
        service.create_collection(
            name="Admitted",
            created_by_user_id=recipient.principal.user_id,
        )
    )
    admitted_document = asyncio.run(
        service.add_document(
            collection_id=admitted.id,
            title="Admitted document",
            text="Shared scope marker from collection A.",
            visible_to=recipient.visible_to,
        )
    )
    run_context = CapabilityContext(
        principal=recipient.principal,
        visible_to=recipient.visible_to,
        knowledge_collection_ids=frozenset({admitted.id}),
    )

    newly_shared = asyncio.run(
        service.create_collection(
            name="Shared later",
            created_by_user_id=owner.principal.user_id,
        )
    )
    newly_shared_document = asyncio.run(
        service.add_document(
            collection_id=newly_shared.id,
            title="Later document",
            text="Shared scope marker from collection B.",
            visible_to=owner.visible_to,
        )
    )
    identity.add_share(
        recipient_user_id=recipient.principal.user_id,
        resource_type="knowledge_collection",
        resource_id=newly_shared.id,
        permission=SharePermission.VIEW,
        granted_by_user_id=owner.principal.user_id,
    )
    assert newly_shared.id in {
        collection.id
        for collection in asyncio.run(
            service.list_collections(visible_to=recipient.visible_to)
        )
    }

    unscoped = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {"query": "Shared scope marker"},
            run_context,
        )
    )
    assert {hit.collection_id for hit in unscoped.hits} == {admitted.id}

    explicit = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {
                "query": "Shared scope marker",
                "collection_ids": [admitted.id],
            },
            run_context,
        )
    )
    assert {hit.collection_id for hit in explicit.hits} == {admitted.id}
    assert asyncio.run(
        registry.invoke(
            "knowledge.document.read",
            {"document_id": admitted_document.id},
            run_context,
        )
    ).id == admitted_document.id

    with pytest.raises(CapabilityError) as search_error:
        asyncio.run(
            registry.invoke(
                "knowledge.search",
                {
                    "query": "Shared scope marker",
                    "collection_ids": [newly_shared.id],
                },
                run_context,
            )
        )
    assert search_error.value.code == "knowledge.collection_not_found"
    assert search_error.value.http_status == 404

    with pytest.raises(CapabilityError) as read_error:
        asyncio.run(
            registry.invoke(
                "knowledge.document.read",
                {"document_id": newly_shared_document.id},
                run_context,
            )
        )
    assert read_error.value.code == "knowledge.document_not_found"
    assert read_error.value.http_status == 404


def test_knowledge_capability_empty_pinned_scope_never_expands_to_visible_data():
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)
    collection = asyncio.run(service.create_collection(name="Visible"))
    document = asyncio.run(
        service.add_document(
            collection_id=collection.id,
            title="Visible document",
            text="This must stay outside an explicitly empty run scope.",
        )
    )
    empty_scope = CapabilityContext(
        principal=_ANON_PRINCIPAL,
        knowledge_collection_ids=frozenset(),
    )

    result = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {"query": "explicitly empty run scope"},
            empty_scope,
        )
    )
    assert result.hits == []

    with pytest.raises(CapabilityError) as search_error:
        asyncio.run(
            registry.invoke(
                "knowledge.search",
                {
                    "query": "explicitly empty run scope",
                    "collection_ids": [collection.id],
                },
                empty_scope,
            )
        )
    assert search_error.value.http_status == 404

    with pytest.raises(CapabilityError) as read_error:
        asyncio.run(
            registry.invoke(
                "knowledge.document.read",
                {"document_id": document.id},
                empty_scope,
            )
        )
    assert read_error.value.http_status == 404


# ------------------------------------------------------------------ #
# catalog: web instant
# ------------------------------------------------------------------ #


def test_web_instant_capability_wraps_one_search_call():
    search = _StubSearch()
    registry = build_capability_registry(search_provider=search)
    out = asyncio.run(
        registry.invoke(
            "web.search.instant",
            {"query": "KI Regulierung", "recency": "week", "max_sources": 1},
            ANON,
        )
    )
    assert out.answer == "Antwort zu KI Regulierung"
    # The full provider citation set survives the capability boundary. The
    # caller may render a compact subset without losing audit evidence.
    assert len(out.sources) == 2
    assert out.parameters["visible_source_limit"] == 1
    assert search.last_kwargs["recency_filter"] == "week"
    assert out.prompt_tokens == 11
    assert out.completion_tokens == 7
    assert out.query_id
    assert out.query == "KI Regulierung"
    assert out.provider == "_StubSearch"
    assert out.started_at
    assert out.finished_at
    assert out.duration_ms >= 0


def test_web_instant_keeps_original_question_separate_from_adaptive_query():
    search = _StubSearch()
    registry = build_capability_registry(search_provider=search)
    context = CapabilityContext(
        principal=_ANON_PRINCIPAL,
        run_id="run_query_lineage",
        question="Was kostet GPT-5.6 Sol je Azure-Region?",
    )

    out = asyncio.run(
        registry.invoke(
            "web.search.instant",
            {"query": "Azure Retail Prices API GPT-5.6 Sol"},
            context,
        )
    )

    assert context.question == "Was kostet GPT-5.6 Sol je Azure-Region?"
    assert out.query == "Azure Retail Prices API GPT-5.6 Sol"


def test_web_instant_preserves_source_metadata_through_ledger_projection():
    from inqtrix.evidence import build_instant_web_search_ledger

    class SourceMetadataSearch(_StubSearch):
        def search(self, query, **kwargs):
            return GroundedSearchResult(
                answer="Alpha is established. [1]",
                sources=[GroundedSource(
                    url="https://alpha.example/report", title="Alpha", rank=1,
                    date="2026-08-01", last_updated="2026-08-02",
                    origin="url_citation", spans=(CitationSpan(22, 25),),
                )],
            )

    registry = build_capability_registry(search_provider=SourceMetadataSearch())
    out = asyncio.run(registry.invoke("web.search.instant", {"query": "Alpha"}, ANON))
    sources = [source.model_dump(mode="json") for source in out.sources]
    ledger = build_instant_web_search_ledger(
        run_id="run_source_metadata", query_id=out.query_id, query=out.query,
        provider=out.provider, answer=out.answer, sources=sources,
    )
    citation = ledger["searches"][out.query_id]["citations"][0]
    assert citation["origin"] == "url_citation"
    assert citation["last_updated"] == "2026-08-02"
    assert citation["annotation_start"] == 22
    assert citation["annotation_end"] == 25


def test_web_instant_keeps_one_source_and_records_every_place_the_answer_cites_it():
    from inqtrix.evidence import build_instant_web_search_ledger

    class TwiceCitedSearch(_StubSearch):
        def search(self, query, **kwargs):
            return GroundedSearchResult(
                answer="Alpha holds 30 %. [1]\nAlpha caps at 28,000. [1]",
                sources=[GroundedSource(
                    url="https://alpha.example/report", title="Alpha", rank=1,
                    origin="url_citation", spans=(CitationSpan(18, 21), CitationSpan(44, 47)),
                )],
            )

    registry = build_capability_registry(search_provider=TwiceCitedSearch())
    out = asyncio.run(registry.invoke("web.search.instant", {"query": "Alpha"}, ANON))
    sources = [source.model_dump(mode="json") for source in out.sources]
    ledger = build_instant_web_search_ledger(
        run_id="run_twice_cited", query_id=out.query_id, query=out.query,
        provider=out.provider, answer=out.answer, sources=sources,
    )
    citations = ledger["searches"][out.query_id]["citations"]
    assert (len(out.sources), [(item["annotation_start"], item["annotation_end"]) for item in citations]) == (
        1, [(18, 21), (44, 47)],
    )
    assert len({item["citation_id"] for item in citations}) == 2


@pytest.mark.parametrize("outer", [False, True])
def test_web_instant_attributes_search_and_preserves_enclosing_task(outer):
    from contextlib import nullcontext
    from inqtrix.observability.context import bind_purpose, current_purpose

    captured = []

    class PurposeSearch(_StubSearch):
        def search(self, query, **kwargs):
            bound = current_purpose()
            captured.append(None if bound is None else
                            (bound.purpose, bound.task_id, bound.task_kind))
            return super().search(query, **kwargs)

    before = current_purpose()
    context = bind_purpose("enclosing", task_id="task-1", task_kind="web_instant") if outer else nullcontext()
    with context:
        registry = build_capability_registry(search_provider=PurposeSearch())
        asyncio.run(registry.invoke("web.search.instant", {"query": "question"}, ANON))
        if outer:
            assert current_purpose().purpose == "enclosing"
    assert current_purpose() is before
    assert captured == [("web_instant", "task-1" if outer else "", "web_instant" if outer else "")]


def test_web_instant_redacts_provider_credential_urls_before_output_and_bundle():
    secret = "provider-output-secret"

    class CredentialSearch(_StubSearch):
        def search(self, query, **kwargs):
            del query, kwargs
            return GroundedSearchResult(
                answer=(
                    "Discovery https://api.example/data?client_secret=" + secret
                ),
                sources=[
                    GroundedSource(
                        url=f"https://api.example/data?x-api-key={secret}",
                        title="Credential-bearing provider source",
                    )
                ],
            )

    registry = build_capability_registry(search_provider=CredentialSearch())
    out = asyncio.run(
        registry.invoke(
            "web.search.instant",
            {"query": "credential redaction"},
            ANON,
        )
    )
    serialized = out.model_dump_json()

    assert secret not in serialized
    assert "client_secret=[REDACTED]" in out.answer
    assert "x-api-key=[REDACTED]" in out.sources[0].url


def test_web_instant_forwards_provider_retry_notice_with_query_context():
    notices: list[dict[str, object]] = []

    class RetryingSearch(_StubSearch):
        retry_callback = None

        @contextmanager
        def observe_retries(self, callback):
            self.retry_callback = callback
            try:
                yield self
            finally:
                self.retry_callback = None

        def search(self, query, **kwargs):
            assert self.retry_callback is not None
            self.retry_callback({
                "attempt": 1,
                "max_attempts": 3,
                "delay_seconds": 1.0,
                "error_code": "upstream_5xx",
            })
            return super().search(query, **kwargs)

    registry = build_capability_registry(search_provider=RetryingSearch())
    context = CapabilityContext(
        principal=None,
        on_provider_retry=lambda notice: notices.append(notice),
    )

    asyncio.run(
        registry.invoke(
            "web.search.instant",
            {"query": "Current market evidence"},
            context,
        )
    )

    assert notices == [
        {
            "attempt": 1,
            "max_attempts": 3,
            "delay_seconds": 1.0,
            "error_code": "upstream_5xx",
            "operation": "web.search.instant",
            "query": "Current market evidence",
        }
    ]


def test_web_instant_rejects_bad_recency():
    registry = build_capability_registry(search_provider=_StubSearch())
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(
            registry.invoke(
                "web.search.instant",
                {"query": "x", "recency": "fortnight"},
                ANON,
            )
        )
    assert excinfo.value.code == "invalid_input"


def test_web_instant_preserves_provider_failure_instead_of_empty_evidence(
) -> None:
    registry = build_capability_registry(search_provider=_FallbackSearch())
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(
            registry.invoke(
                "web.search.instant",
                {"query": "current evidence"},
                ANON,
            )
        )
    assert excinfo.value.code == "upstream_5xx"
    assert excinfo.value.http_status == 503


def test_web_instant_preserves_provider_timeout_code() -> None:
    registry = build_capability_registry(
        search_provider=_ProviderTimeoutFallbackSearch()
    )
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(
            registry.invoke(
                "web.search.instant",
                {"query": "current evidence"},
                ANON,
            )
        )

    assert excinfo.value.code == "provider_timeout"
    assert excinfo.value.http_status == 504


def test_web_capabilities_use_run_resolved_search_provider() -> None:
    """Named-stack tools use the run provider, without a page-reader seam."""

    class LabelledSearch:
        def __init__(self, label: str) -> None:
            self.label = label
            self.calls = 0

        def search(self, query: str, **_kwargs) -> GroundedSearchResult:
            self.calls += 1
            return GroundedSearchResult(answer=f"answer:{self.label}")

    default_search = LabelledSearch("default")
    named_search = LabelledSearch("named")
    registry = build_capability_registry(search_provider=default_search)
    context = replace(
        ANON,
        search_provider=named_search,
    )

    search_output = asyncio.run(
        registry.invoke("web.search.instant", {"query": "price"}, context)
    )
    assert search_output.answer == "answer:named"
    assert named_search.calls == 1
    assert default_search.calls == 0
    assert registry.ids() == ("web.search.instant",)


def test_web_source_read_is_not_registered() -> None:
    registry = build_capability_registry(search_provider=_StubSearch())

    assert "web.source.read" not in registry.ids()
    with pytest.raises(UnknownCapability):
        asyncio.run(
            registry.invoke(
                "web.source.read",
                {"source_ref": "ref_missing"},
                ANON,
            )
        )


def test_registry_registers_only_wired_catalogs():
    # No services wired -> empty manifest (degrades visibly).
    assert build_capability_registry().ids() == ()
    only_web = build_capability_registry(search_provider=_StubSearch())
    assert only_web.ids() == ("web.search.instant",)


def test_knowledge_search_strict_denies_hidden_collection_for_scoped_caller():
    """Agent search is STRICT: a real collection owned by someone else,
    named explicitly by a scoped caller who cannot see it, DENIES the
    whole call (404) — where the legacy debug route would silently drop
    it and search the rest. This is the guarantee the write capabilities
    lean on, so it is tested against a hidden-but-existing collection,
    not merely an absent id."""
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)
    owner = scoped_context("owner-1")
    owned = asyncio.run(
        service.create_collection(
            name="Privat", created_by_user_id=_user_id("owner-1")
        )
    )
    asyncio.run(
        service.add_document(
            collection_id=owned.id,
            title="D",
            text="Geheim.",
            visible_to=owner.visible_to,
        )
    )
    stranger = scoped_context("stranger-2")
    with pytest.raises(CapabilityError) as excinfo:
        asyncio.run(
            registry.invoke(
                "knowledge.search",
                {"query": "Geheim", "collection_ids": [owned.id]},
                stranger,
            )
        )
    assert excinfo.value.http_status == 404
    assert excinfo.value.code == "knowledge.collection_not_found"
    # Sanity: the owner CAN search the same collection (the id is real,
    # the denial above is visibility, not absence).
    owner_out = asyncio.run(
        registry.invoke(
            "knowledge.search",
            {"query": "Geheim", "collection_ids": [owned.id]},
            scoped_context("owner-1"),
        )
    )
    assert owner_out.hits


def test_editor_document_read_capability_bundles_doc_and_comments():
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )

    service = EditorPersistenceService(store=MemoryEditorStore(), durable=False)
    registry = build_capability_registry(editor_service=service)
    asyncio.run(
        service.save_document(
            id="ed_1",
            title="Vermerk",
            content_markdown="Erster Absatz.",
            folder_id=None,
            source="blank",
            source_run_id=None,
            revision=3,
            diff_anchor_markdown=None,
            diff_anchor_updated_at=None,
            created_at=1.0,
            updated_at=1.0,
            caller_user_id=None,
            workspace_id=None,
            visible_to=None,
        )
    )
    out = asyncio.run(
        registry.invoke("editor.document.read", {"document_id": "ed_1"}, ANON)
    )
    assert out.title == "Vermerk"
    assert out.content_markdown == "Erster Absatz."
    assert out.revision == 3
    assert out.comments == []


def test_editor_document_read_capability_uses_exact_collaboration_projection():
    """Agent context consumes the barrier payload, not stored stale Markdown."""
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.collaboration_client import CollaborationProjection
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )

    store = MemoryEditorStore()
    service = EditorPersistenceService(store=store, durable=False)
    registry = build_capability_registry(editor_service=service)
    context = scoped_context("projection-owner")
    document_id = "ed_collaboration_capability"
    asyncio.run(
        service.save_document(
            id=document_id,
            title="Live document",
            content_markdown="# Stored stale projection",
            folder_id=None,
            source="blank",
            source_run_id=None,
            revision=4,
            diff_anchor_markdown=None,
            diff_anchor_updated_at=None,
            created_at=1.0,
            updated_at=1.0,
            caller_user_id=_user_id("projection-owner"),
            workspace_id=None,
            visible_to=context.visible_to,
        )
    )
    document = asyncio.run(store.get_document(document_id))
    store._documents[document_id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
        persisted_sequence=8,
        projection_sequence=7,
    )

    async def project(**kwargs: object) -> CollaborationProjection:
        assert kwargs["document_id"] == document_id
        return CollaborationProjection(
            generation=1,
            sequence=8,
            markdown="# Current collaboration body",
            projection_hash="1" * 64,
            schema_hash="0" * 64,
            authoritative_sequence=8,
        )

    service.bind_collaboration_projector(project)

    output = asyncio.run(
        registry.invoke(
            "editor.document.read",
            {"document_id": document_id},
            context,
        )
    )

    assert output.content_markdown == "# Current collaboration body"


def test_editor_document_read_capability_rejects_stale_collaboration_projection():
    """An unavailable exact projection becomes a visible capability failure."""
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.collaboration_client import CollaborationProjection
    from inqtrix.services.editor_persistence_service import (
        EditorPersistenceService,
    )

    store = MemoryEditorStore()
    service = EditorPersistenceService(store=store, durable=False)
    registry = build_capability_registry(editor_service=service)
    context = scoped_context("projection-owner-failed")
    document_id = "ed_collaboration_capability_failed"
    asyncio.run(
        service.save_document(
            id=document_id,
            title="Live document",
            content_markdown="# Stored stale projection",
            folder_id=None,
            source="blank",
            source_run_id=None,
            revision=4,
            diff_anchor_markdown=None,
            diff_anchor_updated_at=None,
            created_at=1.0,
            updated_at=1.0,
            caller_user_id=_user_id("projection-owner-failed"),
            workspace_id=None,
            visible_to=context.visible_to,
        )
    )
    document = asyncio.run(store.get_document(document_id))
    store._documents[document_id] = replace(  # type: ignore[attr-defined]
        document,
        content_mode="collaboration",
        collaboration_generation=1,
        collaboration_schema_version=1,
        collaboration_schema_hash="0" * 64,
        persisted_sequence=9,
        projection_sequence=8,
    )

    async def project(**kwargs: object) -> CollaborationProjection:
        del kwargs
        return CollaborationProjection(
            generation=1,
            sequence=8,
            markdown="# Not current",
            projection_hash="1" * 64,
            schema_hash="0" * 64,
            authoritative_sequence=9,
        )

    service.bind_collaboration_projector(project)

    with pytest.raises(CapabilityError) as exc_info:
        asyncio.run(
            registry.invoke(
                "editor.document.read",
                {"document_id": document_id},
                context,
            )
        )

    assert exc_info.value.code == "editor.collaboration_projection_unavailable"
    assert exc_info.value.http_status == 503



def _window_document(kind, body):
    if kind == "knowledge":
        service = make_knowledge_service()
        collection = asyncio.run(service.create_collection(name="Source window"))
        document = asyncio.run(service.add_document(collection_id=collection.id, title="Original", text=body))
        assert document.text == body
        return service, build_capability_registry(knowledge_service=service), document.id, "knowledge.document.read", "text"
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.editor_persistence_service import EditorPersistenceService
    service = EditorPersistenceService(store=MemoryEditorStore(), durable=False)
    _save_window_editor(service, body, 1)
    return service, build_capability_registry(editor_service=service), "ed_window", "editor.document.read", "content_markdown"


def _save_window_editor(service, body, revision):
    return asyncio.run(service.save_document(
        id="ed_window", title="Original", content_markdown=body, folder_id=None,
        source="blank", source_run_id=None, revision=revision,
        diff_anchor_markdown=None, diff_anchor_updated_at=None,
        created_at=1.0, updated_at=float(revision), caller_user_id=None,
        workspace_id=None, visible_to=None,
    ))


@pytest.mark.parametrize("kind", ["knowledge", "editor"])
def test_document_windows_reassemble_unicode_originals(kind):
    # This is a text-transfer test, not a parser-normalization test.
    original = "Original " + "ä🙂e\u0301 " * 12_000 + " End"
    _, registry, document_id, capability, text_field = _window_document(kind, original)
    arguments = {"document_id": document_id}
    parts = []
    for _ in range(10):
        result = asyncio.run(registry.invoke(capability, arguments, ANON))
        text = getattr(result, text_field)
        assert len(text) <= 20_000
        assert text == original[result.window.offset:result.window.end]
        assert result.window.total_chars == len(original)
        parts.append(text)
        if result.window.next_read is None:
            break
        arguments = result.window.next_read
    assert result.window.next_read is None
    assert "".join(parts) == original
    assert result.window.end == len(original)


@pytest.mark.parametrize("kind", ["knowledge", "editor"])
def test_document_continuation_requires_hash_and_version(kind):
    _, registry, document_id, capability, _ = _window_document(kind, "x" * 25_000)
    first = asyncio.run(registry.invoke(capability, {"document_id": document_id}, ANON))
    for omitted in ("version", "content_hash"):
        arguments = {**first.window.next_read, omitted: ""}
        with pytest.raises(CapabilityError) as caught:
            asyncio.run(registry.invoke(capability, arguments, ANON))
        assert caught.value.code == "context.invalid_read"
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(registry.invoke(capability, {**first.window.next_read, "version": "another representation"}, ANON))
    assert caught.value.code == "context.version_changed"


def test_editor_windows_reject_a_changed_document():
    service, registry, document_id, capability, _ = _window_document("editor", "x" * 25_000)
    first = asyncio.run(registry.invoke(capability, {"document_id": document_id}, ANON))
    _save_window_editor(service, "y" * 25_000, 2)
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(registry.invoke(capability, first.window.next_read, ANON))
    assert caught.value.code == "context.version_changed"


def test_editor_anchor_search_reaches_beyond_read_windows_and_continues():
    original = "x" * 21_000 + "needle " * 12
    _, registry, document_id, capability, _ = _window_document("editor", original)
    first = asyncio.run(registry.invoke(capability, {"document_id": document_id}, ANON))
    assert "needle" not in first.content_markdown
    arguments = {"document_id": document_id, "query": "needle"}
    offsets = []
    for _ in range(4):
        result = asyncio.run(registry.invoke("editor.document.search", arguments, ANON))
        offsets.extend(hit["offset"] for hit in result.matches)
        if result.next_search is None:
            break
        arguments = result.next_search
    assert offsets == [21_000 + 7 * index for index in range(12)]
    assert result.next_search is None


def test_editor_context_comments_have_their_own_continuation():
    service, registry, document_id, capability, _ = _window_document("editor", "Body")
    asyncio.run(service.save_comments(document_id, visible_to=None, comments=[
        {"id": f"comment_{i}", "comment_markdown": f"Note {i}", "kind": "collect", "status": "open",
         "created_at": float(i + 1), "updated_at": float(i + 1)} for i in range(5)
    ]))
    arguments = {"document_id": document_id, "comment_limit": 2}
    ids = []
    for _ in range(3):
        result = asyncio.run(registry.invoke(capability, arguments, ANON))
        ids.extend(comment.id for comment in result.comments)
        if result.next_comments is None:
            break
        for omitted in ("version", "content_hash"):
            with pytest.raises(CapabilityError) as caught:
                asyncio.run(registry.invoke(capability, {**result.next_comments, omitted: ""}, ANON))
            assert caught.value.code == "context.invalid_read"
        arguments = result.next_comments
    assert set(ids) == {f"comment_{i}" for i in range(5)} and len(ids) == 5
    assert result.next_comments is None


def test_editor_capabilities_enforce_the_injected_target():
    _, registry, document_id, capability, _ = _window_document("editor", "Original")
    context = replace(ANON, target_document_id="another document")
    for capability_id, arguments in [(capability, {"document_id": document_id}),
                                     ("editor.document.search", {"document_id": document_id, "query": "Original"})]:
        with pytest.raises(CapabilityError) as caught:
            asyncio.run(registry.invoke(capability_id, arguments, context))
        assert caught.value.code == "editor.wrong_target"


@pytest.mark.parametrize("kind", ["knowledge", "editor"])
def test_document_window_reports_exact_empty_and_partial_coverage(kind):
    _, registry, document_id, capability, field = _window_document(kind, "Body")
    complete = asyncio.run(registry.invoke(capability, {"document_id": document_id}, ANON))
    assert complete.window.is_full_source is True
    assert complete.window.next_read is None
    partial = asyncio.run(registry.invoke(capability, {"document_id": document_id, "max_chars": 2}, ANON))
    assert getattr(partial, field) == "Bo"
    assert partial.window.is_full_source is False
    assert partial.window.next_read["offset"] == 2
    assert partial.window.next_read["max_chars"] == 2
    at_end = asyncio.run(registry.invoke(capability, {
        **partial.window.next_read, "offset": 4,
    }, ANON))
    assert getattr(at_end, field) == ""
    assert (at_end.window.offset, at_end.window.end, at_end.window.total_chars) == (4, 4, 4)
    assert at_end.window.is_full_source is False
    assert at_end.window.next_read is None


def test_editor_text_continuation_does_not_repeat_comments():
    service, registry, document_id, capability, _ = _window_document("editor", "Text " * 5_000)
    asyncio.run(service.save_comments(document_id, visible_to=None, comments=[{
        "id": "comment_single", "comment_markdown": "Note", "kind": "collect", "status": "open",
        "created_at": 1.0, "updated_at": 1.0,
    }]))
    first = asyncio.run(registry.invoke(capability, {"document_id": document_id}, ANON))
    assert len(first.comments) == 1
    second = asyncio.run(registry.invoke(capability, first.window.next_read, ANON))
    assert second.comments_included is False
    assert second.comments == []
    assert second.next_comments is None


def test_editor_search_continuation_pins_version_and_reports_coverage():
    service, registry, document_id, _, _ = _window_document("editor", "aaaaaa")
    first = asyncio.run(registry.invoke("editor.document.search", {
        "document_id": document_id, "query": "aa", "max_matches": 2,
    }, ANON))
    assert [hit["offset"] for hit in first.matches] == [0, 1]
    assert (first.offset, first.end, first.total_chars, first.total) == (0, 6, 6, 5)
    second = asyncio.run(registry.invoke("editor.document.search", first.next_search, ANON))
    assert [hit["offset"] for hit in second.matches] == [2, 3]
    assert (second.offset, second.end, second.total_chars, second.total) == (2, 6, 6, 3)
    for omitted in ("version", "content_hash"):
        with pytest.raises(CapabilityError) as caught:
            asyncio.run(registry.invoke("editor.document.search", {**first.next_search, omitted: ""}, ANON))
        assert caught.value.code == "context.invalid_read"
    # The same text at a newer revision is still a different source version.
    _save_window_editor(service, "aaaaaa", 2)
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(registry.invoke("editor.document.search", first.next_search, ANON))
    assert caught.value.code == "context.version_changed"


@pytest.mark.parametrize("operation", ["read", "search"])
def test_editor_continuation_rechecks_current_access(operation):
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.editor_persistence_service import EditorPersistenceService

    store = MemoryEditorStore()
    service = EditorPersistenceService(store=store, durable=False)
    _save_window_editor(service, "needle " * 4_000, 1)
    registry = build_capability_registry(editor_service=service)
    capability = "editor.document." + operation
    arguments = {"document_id": "ed_window", **({"query": "needle"} if operation == "search" else {})}
    first = asyncio.run(registry.invoke(capability, arguments, ANON))
    continuation = first.next_search if operation == "search" else first.window.next_read
    # An existing, now privately owned document must not be readable through
    # an earlier unscoped continuation. This exercises the real service check.
    store._documents["ed_window"] = replace(store._documents["ed_window"], created_by_user_id=_user_id("owner"))
    with pytest.raises(CapabilityError) as caught:
        asyncio.run(registry.invoke(capability, continuation, scoped_context("stranger")))
    assert caught.value.code == "editor.document_not_found"
    assert caught.value.http_status == 404
    # The same continuation stays readable for the caller who now owns the
    # document: the capability forwards the CURRENT caller scope, it does not
    # refuse everyone. A refusal of everyone would be silent data loss for
    # the owner and cannot be told apart from the stranger case above.
    owner = asyncio.run(registry.invoke(capability, continuation, scoped_context("owner")))
    assert owner.id == "ed_window"


def test_editor_asserts_transferred_comments_for_the_current_reader():
    """The transcript material re-check: readable document plus every transferred comment in scope."""
    from inqtrix.project.editor_memory import MemoryEditorStore
    from inqtrix.services.editor_persistence_service import EditorPersistenceService

    store = MemoryEditorStore()
    service = EditorPersistenceService(store=store, durable=False)
    _save_window_editor(service, "Body", 1)
    asyncio.run(service.save_comments("ed_window", visible_to=None, comments=[{
        "id": "comment_a", "comment_markdown": "Note", "kind": "collect", "status": "open",
        "created_at": 1.0, "updated_at": 1.0,
    }]))
    document = asyncio.run(service.assert_comments_visible("ed_window", ["comment_a"], visible_to=None))
    assert document.id == "ed_window"
    assert asyncio.run(service.assert_comments_visible("ed_window", [], visible_to=None)).id == "ed_window"
    with pytest.raises(Exception):
        asyncio.run(service.assert_comments_visible("ed_window", ["comment_a", "comment_deleted"], visible_to=None))
    with pytest.raises(Exception):
        asyncio.run(service.assert_comments_visible("ed_missing", [], visible_to=None))
    # A privately owned document is not readable for a stranger, comments or not.
    store._documents["ed_window"] = replace(store._documents["ed_window"], created_by_user_id=_user_id("owner"))
    with pytest.raises(Exception):
        asyncio.run(service.assert_comments_visible("ed_window", ["comment_a"], visible_to=scoped_context("stranger").visible_to))
    assert asyncio.run(service.assert_comments_visible("ed_window", ["comment_a"], visible_to=scoped_context("owner").visible_to)).id == "ed_window"


# ------------------------------------------------------------------ #
# knowledge.document.cite — a quote becomes evidence only through its chunk


def _paragraph(index: int) -> str:
    return " ".join(
        f"Paragraph {index} sentence {line} describes the review step {index * 10 + line}."
        for line in range(1, 24)
    )


def _cite_fixture(text: str | None = None):
    service = make_knowledge_service()
    registry = build_capability_registry(knowledge_service=service)
    collection = asyncio.run(service.create_collection(name="Zitate"))
    body = text if text is not None else "\n\n".join(_paragraph(index) for index in range(1, 6))
    document = asyncio.run(
        service.add_document(collection_id=collection.id, title="Handbuch", text=body)
    )
    window = asyncio.run(
        registry.invoke("knowledge.document.read", {"document_id": document.id}, ANON)
    ).window
    return service, registry, document, window


def _cite(registry, document_id: str, version: str, *quotes: tuple[str, int, int], context=ANON):
    return asyncio.run(
        registry.invoke(
            "knowledge.document.cite",
            {
                "document_id": document_id,
                "version": version,
                "quotes": [{"text": text, "start": start, "end": end} for text, start, end in quotes],
            },
            context,
        )
    ).citations


def test_document_cite_maps_a_window_quote_to_its_verified_chunk():
    service, registry, document, window = _cite_fixture()
    quote = "Paragraph 3 sentence 7 describes the review step 37."
    [citation] = _cite(registry, document.id, window.version, (quote, 0, window.total_chars))
    assert citation.status == "cited"
    chunks = asyncio.run(service.document_chunks(document.id))
    holder = next(chunk for chunk in chunks if quote in chunk.source_text)
    assert citation.evidence["chunk_index"] == holder.chunk_index
    assert citation.evidence["title"] == "Handbuch"
    assert citation.evidence["provenance_status"] == "verified_span"
    assert quote in citation.evidence["excerpt"]
    # The caller's ledger labels evidence; a citation brings no label of its own.
    assert "reference_id" not in citation.evidence


def test_document_cite_rejects_paraphrase():
    _service, registry, document, window = _cite_fixture()
    paraphrase = "Paragraph 3 sentence 7 explains the review step 37."
    [citation] = _cite(registry, document.id, window.version, (paraphrase, 0, window.total_chars))
    assert citation.status == "not_in_window" and citation.evidence is None


def test_document_cite_requires_the_quote_in_its_window():
    """A quote the model remembers from another window is not what it saw."""
    _service, registry, document, window = _cite_fixture()
    late = "Paragraph 5 sentence 20 describes the review step 70."
    [outside] = _cite(registry, document.id, window.version, (late, 0, 1_000))
    assert outside.status == "not_in_window"
    [inside] = _cite(registry, document.id, window.version, (late, 0, window.total_chars))
    assert inside.status == "cited"


def test_document_cite_tolerates_typography_hyphenation_and_page_breaks():
    text = (
        "Intro line.\n\nThe “provider” must docu-\nment every high-\nrisk step.\n\n"
        "3\n\n\x0cThe deployer keeps the logs."
    )
    _service, registry, document, window = _cite_fixture(text)
    citations = _cite(
        registry, document.id, window.version,
        ('The "provider" must document every high-risk step.', 0, window.total_chars),
        # Across the page break: the printed page number and the form feed
        # are extraction artifacts a faithful quote leaves out.
        ("every high-risk step. The deployer keeps the logs.", 0, window.total_chars),
    )
    assert [citation.status for citation in citations] == ["cited", "cited"]


def test_document_cite_quote_across_chunk_boundary_is_not_cited():
    service, registry, document, window = _cite_fixture()
    chunks = sorted(asyncio.run(service.document_chunks(document.id)), key=lambda c: c.chunk_index)
    assert len(chunks) >= 2
    text = document.text
    boundary = chunks[0].source_end  # ASCII text: bytes equal code points
    straddle = text[boundary - 30: chunks[1].source_start + 30]
    [citation] = _cite(registry, document.id, window.version, (straddle, 0, window.total_chars))
    assert citation.status == "spans_chunks" and citation.evidence is None


def test_document_cite_refuses_a_chunk_of_another_content_revision(monkeypatch):
    """A stored chunk whose document hash differs is not current evidence."""
    import dataclasses

    service, registry, document, window = _cite_fixture()
    real = service.document_chunks

    async def stale(document_id, *, visible_to=None):
        return [
            dataclasses.replace(chunk, document_content_hash="0" * 64)
            for chunk in await real(document_id, visible_to=visible_to)
        ]

    monkeypatch.setattr(service, "document_chunks", stale)
    quote = "Paragraph 2 sentence 4 describes the review step 24."
    [citation] = _cite(registry, document.id, window.version, (quote, 0, window.total_chars))
    assert citation.status == "chunk_stale" and citation.evidence is None


def test_document_cite_refuses_a_changed_document_version():
    _service, registry, document, window = _cite_fixture()
    with pytest.raises(CapabilityError) as excinfo:
        _cite(registry, document.id, window.version + "x", ("Paragraph 1", 0, 10))
    assert excinfo.value.code == "context.version_changed"
    assert excinfo.value.http_status == 409


def test_document_cite_and_read_respect_pin_and_visibility():
    """Cite shares read's access path: outside the pin or invisible is 404."""
    identity = MemoryIdentityStore()
    service = make_knowledge_service(identity)
    registry = build_capability_registry(knowledge_service=service)
    owner = scoped_context("cite-owner")
    stranger = scoped_context("cite-stranger")
    pinned = asyncio.run(service.create_collection(name="Pinned", created_by_user_id=owner.principal.user_id))
    other = asyncio.run(service.create_collection(name="Other", created_by_user_id=owner.principal.user_id))
    outside = asyncio.run(service.add_document(
        collection_id=other.id, title="Outside", text="Outside text sentence.", visible_to=owner.visible_to,
    ))
    window = asyncio.run(
        registry.invoke("knowledge.document.read", {"document_id": outside.id}, owner)
    ).window
    run_context = CapabilityContext(
        principal=owner.principal, visible_to=owner.visible_to,
        knowledge_collection_ids=frozenset({pinned.id}),
    )
    for context in (run_context, stranger):
        for capability, payload in (
            ("knowledge.document.read", {"document_id": outside.id}),
            ("knowledge.document.cite", {"document_id": outside.id, "version": window.version,
                                          "quotes": [{"text": "Outside text", "start": 0, "end": 10}]}),
        ):
            with pytest.raises(CapabilityError) as excinfo:
                asyncio.run(registry.invoke(capability, payload, context))
            assert excinfo.value.code == "knowledge.document_not_found"
            assert excinfo.value.http_status == 404


def test_document_cite_refuses_quotes_too_short_to_prove_anything():
    """A letter, a number or a lone word is verbatim in almost any window."""
    _service, registry, document, window = _cite_fixture()
    statuses = [
        citation.status
        for citation in _cite(
            registry, document.id, window.version,
            ("e", 0, window.total_chars), ("9", 0, window.total_chars),
            (" . ", 0, window.total_chars), ("review step", 0, window.total_chars),
            ("describes the review step 37.", 0, window.total_chars),
        )
    ]
    assert statuses == ["too_short", "too_short", "too_short", "too_short", "cited"]


def test_document_cite_refuses_a_chunk_without_verified_span(monkeypatch):
    import dataclasses

    service, registry, document, window = _cite_fixture()
    real = service.document_chunks

    async def unverified(document_id, *, visible_to=None):
        return [
            dataclasses.replace(chunk, source_verified=False)
            for chunk in await real(document_id, visible_to=visible_to)
        ]

    monkeypatch.setattr(service, "document_chunks", unverified)
    quote = "Paragraph 2 sentence 4 describes the review step 24."
    [citation] = _cite(registry, document.id, window.version, (quote, 0, window.total_chars))
    assert citation.status == "chunk_unverified" and citation.evidence is None


def test_document_cite_maps_code_point_windows_onto_byte_spans():
    """Chunk spans are UTF-8 bytes; a window is code points."""
    wide = "\n\n".join(
        "".join(f"倉庫{index}番の記録を確認する。" for index in range(block * 60, block * 60 + 60))
        for block in range(12)
    )
    quote = "The supplier deadline is fourteen days after written notice."
    service, registry, document, window = _cite_fixture(wide + "\n\n" + quote + "\n\n" + wide[:3_000])
    start = document.text.index(quote)
    [citation] = _cite(registry, document.id, window.version, (quote, start - 100, start + 200))
    assert citation.status == "cited" and quote in citation.evidence["excerpt"]
