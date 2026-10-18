"""Retrieval evaluation harness over the knowledge engine.

Runs the German golden set (``golden/``) through the REAL ingestion
and search path (:class:`~inqtrix.services.knowledge_service.KnowledgeService`
— chunking included) against any wired
:class:`~inqtrix.knowledge.stores.ports.KnowledgeProviderContext`, and
computes document-level retrieval metrics. The harness is deliberately
backend-agnostic: the same golden set grades the memory store today
and the Qdrant hybrid store later, which is what makes before/after
comparisons meaningful.

Metrics (documents, binary relevance):

* recall@k — share of queries with at least one relevant document
  among the top-k distinct retrieved documents (any-hit rate; equals
  textbook recall for the single-label majority of the set).
* MRR — mean reciprocal rank of the first relevant document.
* nDCG@k — rank-discounted gain against the ideal ordering.
* multi_complete@k — share of ``multi`` queries with ALL labeled
  documents in the top-k (the any-hit rate is nearly free there).

Eval services chunk with a deliberately small budget
(:data:`EVAL_CHUNK_MAX_CHARS`) so every corpus document splits into
several chunks — the chunking path stays load-bearing and one
document's chunks CAN crowd others out of the candidate list, which
the production default of 2000 chars would degenerate away on this
compact corpus.

``no_evidence`` queries are excluded from retrieval metrics (nothing
relevant exists to retrieve); the answer-side harness
(``answer_harness.py``) grades them: with the sufficiency gate on, they
must end in the honest no-evidence answer.

Any judge model for answer-side metrics must be PINNED: a judge swap
silently re-baselines every threshold.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GOLDEN_DIR = Path(__file__).parent / "golden"
GOLDEN_HARD_DIR = Path(__file__).parent / "golden_hard"
"""Hard tier: EU AI Act split per article (corpus is gitignored and
rebuilt from EUR-Lex via ``golden_hard/build_corpus.py``)."""
GOLDEN_BSI_DIR = Path(__file__).parent / "golden_bsi"
"""BSI tier: IT-Grundschutz Bausteine + C5 criteria (corpus is
gitignored — BSI terms forbid mirroring — and rebuilt from the
official downloads via ``golden_bsi/build_corpus.py``)."""
GOLDEN_DORA_DIR = Path(__file__).parent / "golden_dora"
"""DORA tier: Regulation (EU) 2022/2554 split per article (corpus is
gitignored and rebuilt from the Publications Office Cellar via
``golden_dora/build_corpus.py``). Aimed at the measured
multi-hop/aggregation headroom."""
GOLDEN_DORA_HOLDOUT_DIR = Path(__file__).parent / "golden_dora_holdout"
"""Held-out tier: queries split out of the DORA set that are NEVER
tuned against. It detects overfitting regressions but does not certify a
release. Catching over-fitting requires a set the tuning loop has never seen;
sharing the DORA corpus keeps it rebuildable without duplication."""
GOLDEN_GQUAD_DIR = Path(__file__).parent / "golden_gquad"
"""GermanQuAD tier: everyday-German Wikipedia QA (deepset/germanquad,
CC BY-SA 4.0) — the non-legal counterweight to the four legal tiers.
BOTH corpus and queries are generated locally by
``golden_gquad/build_corpus.py`` and stay gitignored (share-alike
license); only the build script and reviewed baselines are
committed."""
ARTIFACTS_DIR = Path(__file__).parent / "artifacts"
BASELINES_DIR = Path(__file__).parent / "baselines"

RETRIEVAL_CATEGORIES = ("fact", "paraphrase", "exact", "multi")


@dataclass(frozen=True)
class GoldenSet:
    """One selectable golden tier: where queries and corpus live.

    The indirection exists for corpus-SHARING tiers (the held-out
    split reuses the DORA corpus); every other tier keeps the corpus
    in its own ``corpus/`` subdirectory.
    """

    queries_dir: Path
    corpus_dir: Path


GOLDEN_SETS: dict[str, GoldenSet] = {
    "base": GoldenSet(GOLDEN_DIR, GOLDEN_DIR / "corpus"),
    "hard": GoldenSet(GOLDEN_HARD_DIR, GOLDEN_HARD_DIR / "corpus"),
    "bsi": GoldenSet(GOLDEN_BSI_DIR, GOLDEN_BSI_DIR / "corpus"),
    "dora": GoldenSet(GOLDEN_DORA_DIR, GOLDEN_DORA_DIR / "corpus"),
    "dora_holdout": GoldenSet(
        GOLDEN_DORA_HOLDOUT_DIR, GOLDEN_DORA_DIR / "corpus"
    ),
    "gquad": GoldenSet(GOLDEN_GQUAD_DIR, GOLDEN_GQUAD_DIR / "corpus"),
}
"""Registry of selectable tiers (``INQTRIX_EVAL_GOLDEN_SET`` values).

The SINGLE source both the retrieval and the answer eval select from —
an unknown name must fail loudly in both, never silently measure the
base set."""

EVAL_CHUNK_MAX_CHARS = 400
"""Chunk budget for eval services — small enough that every corpus
document produces multiple chunks (see module docstring)."""


@dataclass(frozen=True)
class GoldenQuery:
    """One labeled golden query (document-level relevance)."""

    id: str
    query: str
    relevant: tuple[str, ...]
    category: str


@dataclass(frozen=True)
class QueryResult:
    """Ranked distinct documents one query retrieved."""

    query: GoldenQuery
    ranked_doc_ids: tuple[str, ...]


@dataclass
class EvalReport:
    """Aggregated retrieval metrics plus per-query detail."""

    embedding_model: str
    query_count: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    ndcg_at_5: float
    multi_complete_at_5: float
    per_category_recall_at_5: dict[str, float]
    per_query: list[dict[str, Any]] = field(repr=False, default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "embedding_model": self.embedding_model,
            "query_count": self.query_count,
            "recall_at_1": self.recall_at_1,
            "recall_at_3": self.recall_at_3,
            "recall_at_5": self.recall_at_5,
            "mrr": self.mrr,
            "ndcg_at_5": self.ndcg_at_5,
            "multi_complete_at_5": self.multi_complete_at_5,
            "per_category_recall_at_5": self.per_category_recall_at_5,
            "per_query": self.per_query,
        }


# ------------------------------------------------------------------ #
# Golden set loading
# ------------------------------------------------------------------ #


def load_corpus(
    golden_dir: Path = GOLDEN_DIR,
    *,
    corpus_dir: Path | None = None,
) -> list[tuple[str, str, str]]:
    """Return ``(doc_id, title, text)`` per corpus document.

    Args:
        golden_dir: The tier directory (queries.json lives here).
        corpus_dir: Explicit corpus location for tiers that SHARE a
            corpus (the held-out tier reuses the parent tier's corpus
            instead of duplicating gigabytes of rebuildable text);
            ``None`` uses the tier's own ``corpus/`` subdirectory.
    """
    active_corpus_dir = corpus_dir or (golden_dir / "corpus")
    documents = []
    for path in sorted(active_corpus_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ").strip()
        documents.append((path.stem, title, text))
    if not documents:
        raise RuntimeError(
            f"golden corpus is empty: {active_corpus_dir} — for "
            "rebuildable tiers run the build_corpus.py next to the "
            "queries file first (e.g. "
            f"{golden_dir.name}/build_corpus.py)"
        )
    return documents


def load_queries(
    golden_dir: Path = GOLDEN_DIR,
    *,
    corpus_dir: Path | None = None,
) -> list[GoldenQuery]:
    """Return every golden query (all categories)."""
    payload = json.loads(
        (golden_dir / "queries.json").read_text(encoding="utf-8")
    )
    queries = [
        GoldenQuery(
            id=item["id"],
            query=item["query"],
            relevant=tuple(item["relevant"]),
            category=item["category"],
        )
        for item in payload["queries"]
    ]
    known_ids = {
        doc_id
        for doc_id, _, _ in load_corpus(golden_dir, corpus_dir=corpus_dir)
    }
    for query in queries:
        unknown = set(query.relevant) - known_ids
        if unknown:
            raise RuntimeError(
                f"golden query {query.id} labels unknown documents: {unknown}"
            )
    return queries


def retrieval_queries(
    golden_dir: Path = GOLDEN_DIR,
    *,
    corpus_dir: Path | None = None,
) -> list[GoldenQuery]:
    """Queries that participate in retrieval metrics."""
    return [
        query
        for query in load_queries(golden_dir, corpus_dir=corpus_dir)
        if query.category in RETRIEVAL_CATEGORIES
    ]


# ------------------------------------------------------------------ #
# Metric math (pure functions, unit-tested offline)
# ------------------------------------------------------------------ #


def recall_at_k(results: list[QueryResult], k: int) -> float:
    """Share of queries with >=1 relevant doc in the top-k."""
    if not results:
        return 0.0
    hits = sum(
        1
        for result in results
        if set(result.ranked_doc_ids[:k]) & set(result.query.relevant)
    )
    return hits / len(results)


def mean_reciprocal_rank(results: list[QueryResult]) -> float:
    """Mean of 1/rank of the first relevant document (0 if absent)."""
    if not results:
        return 0.0
    total = 0.0
    for result in results:
        relevant = set(result.query.relevant)
        for rank, doc_id in enumerate(result.ranked_doc_ids, start=1):
            if doc_id in relevant:
                total += 1.0 / rank
                break
    return total / len(results)


def all_relevant_at_k(results: list[QueryResult], k: int) -> float:
    """Share of queries with ALL labeled documents in the top-k."""
    if not results:
        return 0.0
    hits = sum(
        1
        for result in results
        if set(result.query.relevant) <= set(result.ranked_doc_ids[:k])
    )
    return hits / len(results)


def ndcg_at_k(results: list[QueryResult], k: int) -> float:
    """Mean nDCG@k with binary document relevance."""
    if not results:
        return 0.0
    total = 0.0
    for result in results:
        relevant = set(result.query.relevant)
        dcg = sum(
            1.0 / math.log2(rank + 1)
            for rank, doc_id in enumerate(result.ranked_doc_ids[:k], start=1)
            if doc_id in relevant
        )
        ideal_hits = min(len(relevant), k)
        idcg = sum(
            1.0 / math.log2(rank + 1) for rank in range(1, ideal_hits + 1)
        )
        total += dcg / idcg if idcg > 0 else 0.0
    return total / len(results)


# ------------------------------------------------------------------ #
# Runner
# ------------------------------------------------------------------ #


async def run_retrieval_eval(
    service,
    *,
    top_k: int = 5,
    golden_dir: Path = GOLDEN_DIR,
    corpus_dir: Path | None = None,
) -> EvalReport:
    """Ingest the golden corpus and grade retrieval over it.

    Args:
        service: A wired
            :class:`~inqtrix.services.knowledge_service.KnowledgeService`
            (any store/embedding backend).
        top_k: Candidate depth requested per query; document ranking
            uses first-occurrence order of distinct documents.
        golden_dir: The tier directory carrying ``queries.json``.
        corpus_dir: Explicit corpus location for corpus-sharing tiers.
    """
    collection = await service.create_collection(name="eval-golden")
    try:
        return await _run_against_collection(
            service,
            collection.id,
            top_k=top_k,
            golden_dir=golden_dir,
            corpus_dir=corpus_dir,
        )
    finally:
        # Persistent backends (Qdrant) must not accumulate eval junk.
        await service.delete_collection(collection.id)


async def _run_against_collection(
    service,
    collection_id: str,
    *,
    top_k: int,
    golden_dir: Path = GOLDEN_DIR,
    corpus_dir: Path | None = None,
) -> EvalReport:
    document_id_to_golden: dict[str, str] = {}
    for doc_id, title, text in load_corpus(golden_dir, corpus_dir=corpus_dir):
        document = await service.add_document(
            collection_id=collection_id,
            title=title,
            text=text,
            metadata={"golden_id": doc_id},
        )
        document_id_to_golden[document.id] = doc_id

    queries = retrieval_queries(golden_dir, corpus_dir=corpus_dir)
    results: list[QueryResult] = []
    for query in queries:
        candidates = await service.search(
            query=query.query,
            collection_ids=[collection_id],
            # Chunk depth above doc depth: multiple chunks of one doc
            # must not crowd distinct documents out of the top-k.
            top_k=top_k * 3,
        )
        ranked: list[str] = []
        for candidate in candidates:
            golden_id = document_id_to_golden.get(candidate.chunk.document_id)
            if golden_id is not None and golden_id not in ranked:
                ranked.append(golden_id)
            if len(ranked) >= top_k:
                break
        results.append(
            QueryResult(query=query, ranked_doc_ids=tuple(ranked))
        )

    per_category: dict[str, float] = {}
    for category in RETRIEVAL_CATEGORIES:
        subset = [r for r in results if r.query.category == category]
        if subset:
            per_category[category] = round(recall_at_k(subset, 5), 4)

    multi_results = [r for r in results if r.query.category == "multi"]
    return EvalReport(
        embedding_model=service.knowledge.embeddings.default_model,
        query_count=len(results),
        recall_at_1=round(recall_at_k(results, 1), 4),
        recall_at_3=round(recall_at_k(results, 3), 4),
        recall_at_5=round(recall_at_k(results, 5), 4),
        mrr=round(mean_reciprocal_rank(results), 4),
        ndcg_at_5=round(ndcg_at_k(results, 5), 4),
        multi_complete_at_5=round(all_relevant_at_k(multi_results, 5), 4),
        per_category_recall_at_5=per_category,
        per_query=[
            {
                "id": result.query.id,
                "category": result.query.category,
                "relevant": list(result.query.relevant),
                "ranked": list(result.ranked_doc_ids),
            }
            for result in results
        ],
    )


# ------------------------------------------------------------------ #
# Artifacts and baselines
# ------------------------------------------------------------------ #


def write_artifact(report: EvalReport) -> Path:
    """Persist the full report as a timestamped JSON artifact."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_model = report.embedding_model.replace("/", "__")
    path = ARTIFACTS_DIR / f"retrieval-{safe_model}-{time.time_ns() // 1_000_000}.json"
    path.write_text(
        json.dumps(report.to_payload(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def baseline_path(embedding_model: str, *, golden_set: str = "base") -> Path:
    prefix = "" if golden_set == "base" else f"{golden_set}__"
    return BASELINES_DIR / (
        f"{prefix}{embedding_model.replace('/', '__')}.json"
    )


def load_baseline(
    embedding_model: str, *, golden_set: str = "base"
) -> dict[str, float] | None:
    """Committed metric floor for one embedding model, if established."""
    path = baseline_path(embedding_model, golden_set=golden_set)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
