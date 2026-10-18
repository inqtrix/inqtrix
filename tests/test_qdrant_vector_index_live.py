"""Real-server contract tests for ``QdrantVectorIndex`` (gated).

``tests/test_qdrant_vector_index.py`` covers the same class against
``QdrantClient(location=":memory:")`` — qdrant-client's pure-Python
reimplementation. That fake never speaks HTTP, has no segments and no
optimizer, so it stays green through ANY server-side change. This file is the
counterpart that actually holds the running Qdrant to the contract, which is
what makes a server version bump verifiable.

``tests/test_qdrant_store.py`` covers the legacy ``QdrantKnowledgeStore``
(including its conditional-upsert CAS); this file covers the class the server
actually wires in (``inqtrix.server.container``).

Start the dev stack and run with::

    INQTRIX_TEST_QDRANT_URL=http://127.0.0.1:6333 \\
    INQTRIX_TEST_QDRANT_API_KEY=inqtrix-dev-qdrant-key \\
    uv run pytest tests/test_qdrant_vector_index_live.py -v

Each test owns a unique embedding-model name, because the physical collection
name derives from it (``inqtrix_chunks__<slug>``). Nothing here touches a
collection another test or a real deployment created.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio

from inqtrix.knowledge.stores.qdrant_store import (
    QdrantVectorIndex,
    _model_slug,
)
from inqtrix.knowledge.stores.vector_index import ChunkVector, VectorSearchScope

QDRANT_URL = os.environ.get("INQTRIX_TEST_QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("INQTRIX_TEST_QDRANT_API_KEY", "")

pytestmark = pytest.mark.qdrant


@pytest.fixture()
def model_name() -> str:
    """A per-test embedding model id, so each test gets its own collection."""
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture()
async def index(model_name: str) -> Iterator[QdrantVectorIndex]:
    """A dense-only index whose physical collection is dropped afterwards."""
    instance = QdrantVectorIndex(
        url=QDRANT_URL, api_key=QDRANT_API_KEY, sparse="off"
    )
    try:
        yield instance
    finally:
        _drop(instance, model_name)


@pytest_asyncio.fixture()
async def hybrid_index(model_name: str) -> Iterator[QdrantVectorIndex]:
    """A hybrid index; BM25 vectors are computed inside the Qdrant server."""
    instance = QdrantVectorIndex(
        url=QDRANT_URL, api_key=QDRANT_API_KEY, sparse="bm25_german"
    )
    try:
        yield instance
    finally:
        _drop(instance, model_name)


def _drop(instance: QdrantVectorIndex, model_name: str) -> None:
    name = _model_slug(model_name)
    try:
        if instance._client.collection_exists(name):
            instance._client.delete_collection(name)
    except Exception:  # noqa: BLE001 — teardown must not mask a test failure
        pass


@pytest.mark.asyncio
async def test_server_matches_explicit_null_lineage_as_empty(
    index: QdrantVectorIndex, model_name: str
) -> None:
    """``IsEmptyCondition`` must mean absent OR JSON-null on the real server.

    The scope filter leans on this in three retrieval branches and in the
    registry CAS, and modern points write ``generation_id``/``revision_id``
    as an explicit ``None`` rather than omitting them. If a server version
    narrowed ``is_empty`` to absent-only, pre-lineage points would silently
    drop out of retrieval with no error anywhere — a recall regression that
    the in-process fake cannot reproduce.
    """
    await index.ensure_model(embedding_model=model_name, embedding_dim=2)
    await index.upsert(
        embedding_model=model_name,
        collection_id="kc_active",
        document_id="kd_legacy",
        # Explicit None on both lineage fields -> written as JSON null.
        vectors=[ChunkVector("kch_legacy", (1.0, 0.0))],
    )
    await index.upsert(
        embedding_model=model_name,
        collection_id="kc_active",
        document_id="kd_active",
        vectors=[
            ChunkVector(
                "kch_active",
                (0.9, 0.1),
                generation_id="gen_active",
                revision_id="rev_active",
            ),
            # Missing lineage but NOT migration-verified: must stay excluded.
            ChunkVector("kch_unverified", (0.95, 0.05)),
        ],
    )

    hits = await index.search(
        embedding_model=model_name,
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="kc_active",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
                legacy_payload_chunk_ids=("kch_legacy",),
            )
        ],
        top_k=10,
    )

    assert {hit.chunk_id for hit in hits} == {"kch_active", "kch_legacy"}


@pytest.mark.asyncio
async def test_server_filters_inactive_generation_and_revision(
    index: QdrantVectorIndex, model_name: str
) -> None:
    """Only the active generation AND revision may reach ranking."""
    await index.ensure_model(embedding_model=model_name, embedding_dim=2)
    await index.upsert(
        embedding_model=model_name,
        collection_id="kc_active",
        document_id="kd_active",
        vectors=[
            ChunkVector(
                "kch_active",
                (0.8, 0.2),
                generation_id="gen_active",
                revision_id="rev_active",
            ),
            ChunkVector(
                "kch_staged_generation",
                (1.0, 0.0),
                generation_id="gen_staged",
                revision_id="rev_active",
            ),
            ChunkVector(
                "kch_staged_revision",
                (1.0, 0.0),
                generation_id="gen_active",
                revision_id="rev_staged",
            ),
        ],
    )

    hits = await index.search(
        embedding_model=model_name,
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="kc_active",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
            )
        ],
        top_k=10,
    )

    # The two staged points rank HIGHER by cosine; the filter, not the score,
    # is what must exclude them.
    assert [hit.chunk_id for hit in hits] == ["kch_active"]


@pytest.mark.asyncio
async def test_ensure_model_is_idempotent_against_the_server(
    index: QdrantVectorIndex, model_name: str
) -> None:
    """Repeat ``ensure_model`` must stay a no-op.

    It runs on every indexing pass, and it re-issues ``create_payload_index``
    for each payload field unconditionally. If a server version made a repeat
    identical create an error instead of a no-op, every indexing run would
    fail — so this is asserted against the server, not assumed.
    """
    await index.ensure_model(embedding_model=model_name, embedding_dim=2)
    await index.ensure_model(embedding_model=model_name, embedding_dim=2)
    await index.ensure_model(embedding_model=model_name, embedding_dim=2)

    await index.upsert(
        embedding_model=model_name,
        collection_id="kc_active",
        document_id="kd_active",
        vectors=[
            ChunkVector(
                "kch_active",
                (1.0, 0.0),
                generation_id="gen_active",
                revision_id="rev_active",
            )
        ],
    )
    hits = await index.search(
        embedding_model=model_name,
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="kc_active",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
            )
        ],
        top_k=5,
    )

    assert [hit.chunk_id for hit in hits] == ["kch_active"]


@pytest.mark.asyncio
async def test_hybrid_ranking_places_the_lexical_match_first(
    hybrid_index: QdrantVectorIndex, model_name: str
) -> None:
    """Server-side IDF + RRF must still rank the exact-term match on top.

    Ranking is computed inside Qdrant (``Modifier.IDF`` plus
    ``FusionQuery(Fusion.RRF)``), so it can drift across server versions with
    no error raised. This asserts RELATIVE order only — absolute scores are
    not comparable across versions and must never become an acceptance
    criterion.
    """
    await hybrid_index.ensure_model(embedding_model=model_name, embedding_dim=2)
    await hybrid_index.upsert(
        embedding_model=model_name,
        collection_id="kc_active",
        document_id="kd_active",
        vectors=[
            ChunkVector(
                "kch_exact",
                (0.6, 0.8),
                text="Der Server DB-PROD-037 speichert die Sicherungskopien.",
                generation_id="gen_active",
                revision_id="rev_active",
            ),
            ChunkVector(
                "kch_unrelated",
                (0.6, 0.8),
                text="Die Kantine ist von zwoelf bis vierzehn Uhr geoeffnet.",
                generation_id="gen_active",
                revision_id="rev_active",
            ),
        ],
    )

    hits = await hybrid_index.hybrid_search(
        embedding_model=model_name,
        query_text="DB-PROD-037",
        query_embedding=[0.6, 0.8],
        scopes=[
            VectorSearchScope(
                collection_id="kc_active",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
            )
        ],
        top_k=5,
    )

    # Identical dense vectors, so the lexical branch is the only signal that
    # can separate them: the rare identifier must win.
    assert [hit.chunk_id for hit in hits][0] == "kch_exact"
    assert {hit.chunk_id for hit in hits} == {"kch_exact", "kch_unrelated"}
