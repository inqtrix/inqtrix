"""Gated retrieval-quality eval against a real embedding endpoint.

Activated by ``INQTRIX_EVAL_EMBEDDING_BASE_URL`` (plus key/model
variables); never part of the offline default run. Quality is graded
against the committed per-model baseline under ``baselines/``:

* baseline present — metrics must not regress below it (small
  tolerance for embedding-endpoint nondeterminism); a full JSON
  artifact is written either way.
* baseline absent — the run writes the artifact and SKIPS with an
  explicit pointer: establishing a baseline is a deliberate, reviewed
  commit (copy the artifact's aggregate metrics), never an automatic
  side effect of a green test.

This suite is the gate the plan requires before locking embedding
models or switching retrieval backends (memory -> Qdrant hybrid):
run it once per candidate configuration and compare artifacts.
"""

from __future__ import annotations

import os

import pytest

from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.embeddings import (
    AzureOpenAIEmbeddings,
    LiteLLMEmbeddings,
)
from inqtrix.services.knowledge_service import KnowledgeService

from tests.eval.harness import (
    EVAL_CHUNK_MAX_CHARS,
    GOLDEN_SETS,
    baseline_path,
    load_baseline,
    run_retrieval_eval,
    write_artifact,
)

EVAL_GOLDEN_SET = os.environ.get("INQTRIX_EVAL_GOLDEN_SET", "base")
if EVAL_GOLDEN_SET not in GOLDEN_SETS:
    # A typo must not silently measure the BASE set and then degrade
    # the baseline gate into a skip.
    raise RuntimeError(
        f"unknown INQTRIX_EVAL_GOLDEN_SET={EVAL_GOLDEN_SET!r}; "
        f"valid: {sorted(GOLDEN_SETS)}"
    )
EVAL_GOLDEN = GOLDEN_SETS[EVAL_GOLDEN_SET]

EVAL_PROVIDER = os.environ.get(
    "INQTRIX_EVAL_EMBEDDING_PROVIDER", "openai_compatible"
)
EVAL_BASE_URL = os.environ.get("INQTRIX_EVAL_EMBEDDING_BASE_URL", "")
EVAL_API_KEY = os.environ.get("INQTRIX_EVAL_EMBEDDING_API_KEY", "")
EVAL_MODEL = os.environ.get(
    "INQTRIX_EVAL_EMBEDDING_MODEL", "text-embedding-3-small"
)
# Azure path: reuse the deployment's established variables (load `.env`
# through uv or export it in a standard pip/plain-Python environment) — same
# fallback chain as the settings bridge.
EVAL_AZURE_ENDPOINT = (
    os.environ.get("INQTRIX_EVAL_AZURE_ENDPOINT", "")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "")
)
EVAL_AZURE_API_KEY = (
    os.environ.get("INQTRIX_EVAL_AZURE_API_KEY", "")
    or os.environ.get("AZURE_AI_PROJECT_API_KEY", "")
    or os.environ.get("AZURE_OPENAI_API_KEY", "")
)

_eval_configured = (
    bool(EVAL_AZURE_ENDPOINT and EVAL_AZURE_API_KEY)
    if EVAL_PROVIDER == "azure"
    else bool(EVAL_BASE_URL)
)

pytestmark = pytest.mark.skipif(
    not _eval_configured,
    reason=(
        "real-embedding eval not configured "
        "(INQTRIX_EVAL_EMBEDDING_BASE_URL, or "
        "INQTRIX_EVAL_EMBEDDING_PROVIDER=azure with Azure endpoint/key)"
    ),
)

BASELINE_TOLERANCE = 0.05
"""Allowed dip below the committed baseline before the gate fails.
Be aware of the arithmetic: 0.05 absolute over 44 queries means TWO
whole queries may silently flip on every recall metric, and with no
ratchet the quality may sit at floor minus tolerance indefinitely —
tighten this once a real-model baseline shows how noisy the endpoint
actually is."""

GATED_METRICS = (
    "recall_at_1",
    "recall_at_3",
    "recall_at_5",
    "mrr",
    "ndcg_at_5",
    "multi_complete_at_5",
)


def make_eval_embeddings():
    if EVAL_PROVIDER == "azure":
        return AzureOpenAIEmbeddings(
            api_key=EVAL_AZURE_API_KEY,
            azure_endpoint=EVAL_AZURE_ENDPOINT,
            default_model=EVAL_MODEL,
        )
    return LiteLLMEmbeddings(
        api_key=EVAL_API_KEY,
        base_url=EVAL_BASE_URL,
        default_model=EVAL_MODEL,
    )


def make_eval_store():
    """Memory by default; Qdrant via INQTRIX_EVAL_VECTOR_BACKEND=qdrant
    (+ INQTRIX_EVAL_QDRANT_URL / _API_KEY / _SPARSE) for before/after
    backend comparisons against the same baseline."""
    if os.environ.get("INQTRIX_EVAL_VECTOR_BACKEND", "memory") == "qdrant":
        from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore

        return QdrantKnowledgeStore(
            url=os.environ.get(
                "INQTRIX_EVAL_QDRANT_URL", "http://127.0.0.1:6333"
            ),
            api_key=os.environ.get("INQTRIX_EVAL_QDRANT_API_KEY", ""),
            sparse=os.environ.get("INQTRIX_EVAL_SPARSE", "bm25_german"),
        )
    return MemoryKnowledgeStore()


def make_eval_reranker():
    """Optional rerank stage: INQTRIX_EVAL_RERANKER=cohere reuses the
    deployment's INQTRIX_RERANKER_* variables; ``llm`` wires the
    listwise fallback through the deployment's Azure chat model (run
    with ``uv run --env-file .env``)."""
    variant = os.environ.get("INQTRIX_EVAL_RERANKER", "none")
    if variant == "cohere":
        from inqtrix.providers.rerankers import CohereRerank

        return CohereRerank(
            api_key=os.environ.get("INQTRIX_RERANKER_API_KEY", ""),
            base_url=os.environ.get("INQTRIX_RERANKER_BASE_URL", ""),
            default_model=os.environ.get("INQTRIX_RERANKER_MODEL", ""),
        )
    if variant == "llm":
        from inqtrix.providers.azure import AzureOpenAILLM
        from inqtrix.providers.rerankers import LLMReranker

        return LLMReranker(
            AzureOpenAILLM(
                azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
                api_key=os.environ["AZURE_OPENAI_API_KEY"],
                default_model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
            )
        )
    if variant != "none":
        raise RuntimeError(
            f"unknown INQTRIX_EVAL_RERANKER={variant!r}; "
            "valid: none, cohere, llm"
        )
    return None


def make_eval_contextualizer():
    """Optional contextual retrieval: INQTRIX_EVAL_CONTEXTUALIZE=on
    wires the deployment's Azure chat model as the per-document
    contextualizer (run with ``uv run --env-file .env``)."""
    if os.environ.get("INQTRIX_EVAL_CONTEXTUALIZE", "off") != "on":
        return None
    from inqtrix.knowledge.contextualize import LLMChunkContextualizer
    from inqtrix.providers.azure import AzureOpenAILLM

    return LLMChunkContextualizer(
        AzureOpenAILLM(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            default_model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        )
    )


def make_real_service() -> KnowledgeService:
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=make_eval_embeddings(),
            store=make_eval_store(),
            default_top_k=5,
            reranker=make_eval_reranker(),
            rerank_candidate_depth=30,
            contextualizer=make_eval_contextualizer(),
        ),
        chunk_max_chars=EVAL_CHUNK_MAX_CHARS,
        max_document_chars=100_000,
    )


@pytest.mark.asyncio
async def test_retrieval_quality_holds_the_committed_baseline():
    report = await run_retrieval_eval(
        make_real_service(),
        top_k=5,
        golden_dir=EVAL_GOLDEN.queries_dir,
        corpus_dir=EVAL_GOLDEN.corpus_dir,
    )
    artifact = write_artifact(report)

    # Baselines are keyed on the VERBATIM model id — use the same
    # spelling here as in the committed baseline (no provider-prefix
    # aliases like 'openai/...').
    baseline = load_baseline(
        report.embedding_model, golden_set=EVAL_GOLDEN_SET
    )
    if baseline is None:
        pytest.skip(
            f"no committed baseline for {report.embedding_model!r}; "
            f"artifact written to {artifact} — review it and commit the "
            "aggregate metrics to "
            f"{baseline_path(report.embedding_model, golden_set=EVAL_GOLDEN_SET)}"
        )

    # A baseline missing gated keys would silently un-gate them
    # (No Silent Fallbacks) — incomplete baselines fail loudly.
    missing = [m for m in GATED_METRICS if m not in baseline]
    assert not missing, (
        f"baseline {baseline_path(report.embedding_model, golden_set=EVAL_GOLDEN_SET)} lacks gated "
        f"metrics {missing}; re-establish it from a fresh artifact"
    )

    failures = []
    payload = report.to_payload()
    for metric in GATED_METRICS:
        floor = baseline.get(metric)
        if floor is None:
            continue
        if payload[metric] < floor - BASELINE_TOLERANCE:
            failures.append(
                f"{metric}={payload[metric]} below baseline {floor}"
            )
    assert not failures, (
        f"retrieval quality regressed for {report.embedding_model!r}: "
        f"{failures}; full report: {artifact}"
    )
