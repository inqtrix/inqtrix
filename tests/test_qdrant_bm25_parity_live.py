"""Real-server BM25 parity: server-side inference vs recorded client goldens.

The sparse branch sends ``models.Document`` objects that the Qdrant server
expands in its core (>= 1.15.3). Collections can still hold BM25 vectors
computed client-side (fastembed's ``Qdrant/bm25``, ``language="german"``).
The golden maps below are that client-side output recorded ONCE with
fastembed 0.8.0 at its defaults (k=1.2, b=0.75, avg_len=256,
token_max_length=40) — they are the proof obligation that the server's
tokenizer/stemmer/hash pipeline produces the SAME vectors, i.e. that
collections indexed client-side stay searchable without a reindex and
mixed-generation fleets are safe mid-deploy.

Test A compares stored document vectors index-by-index against the goldens
(the no-reindex proof, including the two designed edge cases: a > 40-char
German compound that fastembed DROPPED client-side, and a stopword-only text
that must yield an EMPTY sparse vector). The store pins
``max_token_len: 40`` in the Document options precisely for the compound
case — the server default KEEPS long tokens, which would break parity there.
Test B writes a point carrying the literal golden vector — a client-side
point — and finds it through the Document query path (the mixed-generation
proof; it also covers query-side
hashing indirectly, since a query only matches when its token ids equal the
stored ones).

Re-record the goldens (fastembed is not a project dependency) with::

    uv run --with fastembed==0.8.0 python - <<'EOF'
    from fastembed import SparseTextEmbedding
    enc = SparseTextEmbedding(model_name="Qdrant/bm25", language="german")
    for text in [...]:  # the GOLDENS keys below
        doc = list(enc.embed([text]))[0]
        query = list(enc.query_embed(text))[0]
        print(dict(sorted(zip(map(int, doc.indices), map(float, doc.values)))))
        print(dict(sorted(zip(map(int, query.indices), map(float, query.values)))))
    EOF

Start the dev stack and run with::

    INQTRIX_TEST_QDRANT_URL=http://127.0.0.1:6333 \\
    INQTRIX_TEST_QDRANT_API_KEY=inqtrix-dev-qdrant-key \\
    uv run pytest tests/test_qdrant_bm25_parity_live.py -v
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest
import pytest_asyncio

qdrant_client = pytest.importorskip("qdrant_client")
from qdrant_client import models

from inqtrix.knowledge.stores.qdrant_store import (
    QdrantVectorIndex,
    _model_slug,
    _point_uuid,
)
from inqtrix.knowledge.stores.vector_index import ChunkVector, VectorSearchScope

QDRANT_URL = os.environ.get("INQTRIX_TEST_QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("INQTRIX_TEST_QDRANT_API_KEY", "")

pytestmark = pytest.mark.qdrant

# fastembed==0.8.0, model=Qdrant/bm25, language=german; defaults k=1.2,
# b=0.75, avg_len=256, token_max_length=40. {text: (document_map, query_map)}
# with maps of {token_id: weight}.
GOLDENS: dict[str, tuple[dict[int, float], dict[int, float]]] = {
    "Die Verpflegungspauschale beträgt 28 Euro pro Reisetag.": (
        {
            465439522: 1.6652868125369606,
            622429534: 1.6652868125369606,
            833760702: 1.6652868125369606,
            894837388: 1.6652868125369606,
            1512182177: 1.6652868125369606,
            2112545234: 1.6652868125369606,
        },
        {
            465439522: 1.0,
            622429534: 1.0,
            833760702: 1.0,
            894837388: 1.0,
            1512182177: 1.0,
            2112545234: 1.0,
        },
    ),
    "Der Server DB-PROD-037 speichert die Sicherungskopien!": (
        {
            190815531: 1.6652868125369606,
            264009555: 1.6652868125369606,
            950561027: 1.6652868125369606,
            1626003955: 1.6652868125369606,
            1774407789: 1.6652868125369606,
            1942436332: 1.6652868125369606,
        },
        {
            190815531: 1.0,
            264009555: 1.0,
            950561027: 1.0,
            1626003955: 1.0,
            1774407789: 1.0,
            1942436332: 1.0,
        },
    ),
    "Straße, Maßnahme und Grüße: schließen, größer, weiß.": (
        {
            403575724: 1.6652868125369606,
            631207143: 1.6652868125369606,
            1164261607: 1.6652868125369606,
            1589523249: 1.6652868125369606,
            1795522012: 1.6652868125369606,
            1962405504: 1.6652868125369606,
        },
        {
            403575724: 1.0,
            631207143: 1.0,
            1164261607: 1.0,
            1589523249: 1.0,
            1795522012: 1.0,
            1962405504: 1.0,
        },
    ),
    # The compound exceeds token_max_length=40 and was DROPPED client-side;
    # only two short-token stems remain. THE likeliest divergence spot.
    (
        "Die Grundstücksverkehrsgenehmigungszuständigkeits"
        "übertragungsverordnung wurde geändert."
    ): (
        {55023666: 1.6832038254632398, 1162941628: 1.6832038254632398},
        {55023666: 1.0, 1162941628: 1.0},
    ),
    # Stopwords only: the sparse vector must be EMPTY, and the upsert must
    # still succeed (chunks like this exist in real corpora).
    "und oder aber die der das ein eine mit von zu": ({}, {}),
}

_TOP_TEXT = "Die Verpflegungspauschale beträgt 28 Euro pro Reisetag."


@pytest.fixture()
def model_name() -> str:
    """A per-test embedding model id, so each test gets its own collection."""
    return f"itest-{uuid.uuid4().hex[:12]}"


@pytest_asyncio.fixture()
async def hybrid_index(model_name: str) -> Iterator[QdrantVectorIndex]:
    """A hybrid index; BM25 vectors are computed inside the Qdrant server."""
    instance = QdrantVectorIndex(
        url=QDRANT_URL, api_key=QDRANT_API_KEY, sparse="bm25_german"
    )
    try:
        yield instance
    finally:
        name = _model_slug(model_name)
        try:
            if instance._client.collection_exists(name):
                instance._client.delete_collection(name)
        except Exception:  # noqa: BLE001 — teardown must not mask a failure
            pass


def _stored_sparse_map(
    instance: QdrantVectorIndex, model_name: str, chunk_id: str
) -> dict[int, float]:
    records = instance._client.retrieve(
        collection_name=_model_slug(model_name),
        ids=[_point_uuid(chunk_id)],
        with_vectors=True,
    )
    assert len(records) == 1, f"point for {chunk_id!r} not stored"
    sparse = (records[0].vector or {}).get("sparse")
    if sparse is None:
        return {}
    return dict(zip((int(i) for i in sparse.indices), (float(v) for v in sparse.values)))


@pytest.mark.asyncio
async def test_server_inference_matches_recorded_client_goldens(
    hybrid_index: QdrantVectorIndex, model_name: str
) -> None:
    """Document parity: stored server-side vectors == fastembed goldens."""
    await hybrid_index.ensure_model(embedding_model=model_name, embedding_dim=2)
    texts = list(GOLDENS)
    await hybrid_index.upsert(
        embedding_model=model_name,
        collection_id="kc_parity",
        document_id="kd_parity",
        vectors=[
            ChunkVector(
                f"kch_{position}",
                (1.0, 0.0),
                text=text,
                generation_id="gen",
                revision_id="rev",
            )
            for position, text in enumerate(texts)
        ],
    )

    for position, text in enumerate(texts):
        expected, _query = GOLDENS[text]
        stored = _stored_sparse_map(hybrid_index, model_name, f"kch_{position}")
        assert stored.keys() == expected.keys(), (
            f"token ids diverge for {text!r}: "
            f"server-only={sorted(stored.keys() - expected.keys())}, "
            f"client-only={sorted(expected.keys() - stored.keys())}"
        )
        for token_id, weight in expected.items():
            assert stored[token_id] == pytest.approx(weight, rel=1e-6), (
                f"TF weight diverges for {text!r} token {token_id}"
            )


@pytest.mark.asyncio
async def test_document_query_finds_pre_swap_golden_vector(
    hybrid_index: QdrantVectorIndex, model_name: str
) -> None:
    """Mixed generations: a pre-swap point stays the top hit for its terms.

    The old point carries the literal golden vector (as every point written
    before the swap does); the distractor is written through the new
    Document path. Both share one dense vector, so the dense ranks tie and
    RRF is decided by the sparse branch alone — the old point wins iff the
    query's server-inferred token ids equal the client-recorded ones.
    """
    await hybrid_index.ensure_model(embedding_model=model_name, embedding_dim=2)
    golden_doc, _query = GOLDENS[_TOP_TEXT]
    hybrid_index._client.upsert(
        collection_name=_model_slug(model_name),
        points=[
            models.PointStruct(
                id=_point_uuid("kch_old_generation"),
                vector={
                    "dense": [1.0, 0.0],
                    "sparse": models.SparseVector(
                        indices=list(golden_doc.keys()),
                        values=list(golden_doc.values()),
                    ),
                },
                payload={
                    "collection_id": "kc_parity",
                    "document_id": "kd_old",
                    "chunk_id": "kch_old_generation",
                    "generation_id": "gen",
                    "revision_id": "rev",
                },
            )
        ],
        wait=True,
    )
    await hybrid_index.upsert(
        embedding_model=model_name,
        collection_id="kc_parity",
        document_id="kd_new",
        vectors=[
            ChunkVector(
                "kch_distractor",
                (1.0, 0.0),
                text="Der Grenzwert für Bewirtungskosten liegt bei 60 Euro.",
                generation_id="gen",
                revision_id="rev",
            )
        ],
    )

    hits = await hybrid_index.hybrid_search(
        embedding_model=model_name,
        query_text="Verpflegungspauschale",
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="kc_parity",
                generation_id="gen",
                active_revision_ids=("rev",),
            )
        ],
        top_k=2,
    )

    assert [hit.chunk_id for hit in hits][0] == "kch_old_generation"
