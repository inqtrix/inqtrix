"""Gated live answer eval: gate honesty over the golden set.

Requires the Azure deployment (load ``.env`` through uv or export it in a
standard pip/plain-Python environment): embeddings (azure provider fallbacks)
plus the chat deployment from ``AZURE_OPENAI_DEPLOYMENT_NAME``. Roughly 50
algorithm runs — each one embed call, one or two gate calls, and at most one
answer call — so this stays out of the offline default suite by design.

Gate thresholds (absolute, behaviour-defining rather than
baseline-relative): the six ``no_evidence`` queries must mostly be
refused, answerable queries must almost never be refused, answered
queries must cite. The committed answer baseline tightens these over
time per model.
"""

from __future__ import annotations

import os

import pytest

from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.base import ProviderContext
from inqtrix.providers.embeddings import AzureOpenAIEmbeddings
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import Settings

from tests.contract._app import StubSearch
from tests.eval.answer_harness import (
    answer_baseline_path,
    ingest_golden_corpus,
    load_answer_baseline,
    run_answer_eval,
    write_answer_artifact,
)
from tests.eval.harness import EVAL_CHUNK_MAX_CHARS, GOLDEN_SETS

EVAL_GOLDEN_SET = os.environ.get("INQTRIX_EVAL_GOLDEN_SET", "base")
if EVAL_GOLDEN_SET not in GOLDEN_SETS:
    # Mirror the retrieval eval: a typo must fail loudly, never
    # silently grade the base set.
    raise RuntimeError(
        f"unknown INQTRIX_EVAL_GOLDEN_SET={EVAL_GOLDEN_SET!r}; "
        f"valid: {sorted(GOLDEN_SETS)}"
    )
EVAL_GOLDEN = GOLDEN_SETS[EVAL_GOLDEN_SET]

EVAL_PROFILE = os.environ.get("INQTRIX_EVAL_KNOWLEDGE_PROFILE", "standard")
# Fail at import like the tier selector — a profile typo must not
# crash 30 queries deep into a paid live run.
from inqtrix.knowledge.profiles import parse_knowledge_profile  # noqa: E402

parse_knowledge_profile(EVAL_PROFILE)

AZURE_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
LLM_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
EMBED_ENDPOINT = (
    os.environ.get("INQTRIX_EVAL_AZURE_ENDPOINT", "")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT", "")
)
EMBED_KEY = (
    os.environ.get("INQTRIX_EVAL_AZURE_API_KEY", "")
    or os.environ.get("AZURE_AI_PROJECT_API_KEY", "")
    or AZURE_KEY
)
EMBED_MODEL = os.environ.get(
    "INQTRIX_EVAL_EMBEDDING_MODEL", "text-embedding-3-large"
)

pytestmark = pytest.mark.skipif(
    not (AZURE_ENDPOINT and AZURE_KEY and LLM_DEPLOYMENT and EMBED_ENDPOINT),
    reason=(
        "live answer eval needs AZURE_OPENAI_ENDPOINT/_API_KEY/"
        "_DEPLOYMENT_NAME plus the embeddings endpoint "
        "(load the required values from .env before the test)"
    ),
)

# Behaviour-defining floors. abstention < 0.5 would mean the gate
# mostly fails its purpose; false refusals are the costlier error for
# users, hence the tighter bound.
ABSTENTION_FLOOR = 0.5
FALSE_REFUSAL_CEILING = 0.10
CITATION_FLOOR = 0.9


def make_run_algorithm():
    from inqtrix.providers.azure import AzureOpenAILLM

    embeddings = AzureOpenAIEmbeddings(
        api_key=EMBED_KEY,
        azure_endpoint=EMBED_ENDPOINT,
        default_model=EMBED_MODEL,
    )
    knowledge = KnowledgeProviderContext(
        embeddings=embeddings,
        store=MemoryKnowledgeStore(),
        default_top_k=5,
    )
    service = KnowledgeService(
        knowledge=knowledge,
        chunk_max_chars=EVAL_CHUNK_MAX_CHARS,
        max_document_chars=100_000,
    )
    collection_id = ingest_golden_corpus(
        service,
        golden_dir=EVAL_GOLDEN.queries_dir,
        corpus_dir=EVAL_GOLDEN.corpus_dir,
    )
    algorithm = KnowledgeAlgorithm(knowledge=knowledge, gate_enabled=True)

    llm = AzureOpenAILLM(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_KEY,
        default_model=LLM_DEPLOYMENT,
    )
    settings = Settings()
    runtime = RuntimeContext(
        settings=settings,
        registry=None,
        providers=ProviderContext(llm=llm, search=StubSearch()),
        strategies=None,
    )

    def run_algorithm(question: str, scoped_collection: str):
        context = RunContext(
            providers=runtime.providers,
            strategies=None,
            agent_settings=settings.agent,
        )
        return algorithm.run(
            RunRequest(
                mode="knowledge",
                question=question,
                knowledge_filters={
                    "collection_ids": [scoped_collection],
                    "profile": EVAL_PROFILE,
                },
            ),
            runtime=runtime,
            context=context,
        )

    return run_algorithm, collection_id


def test_gate_honesty_over_the_golden_set():
    run_algorithm, collection_id = make_run_algorithm()
    report = run_answer_eval(
        run_algorithm=run_algorithm,
        collection_id=collection_id,
        llm_model=LLM_DEPLOYMENT,
        embedding_model=EMBED_MODEL,
        golden_set=EVAL_GOLDEN_SET,
        golden_dir=EVAL_GOLDEN.queries_dir,
        corpus_dir=EVAL_GOLDEN.corpus_dir,
        profile=EVAL_PROFILE,
    )
    artifact = write_answer_artifact(report)

    failures = []
    # Tiers without no_evidence queries (bsi/dora) have nothing to
    # abstain from — the floor only applies where the metric exists.
    if (
        report.abstention_rate is not None
        and report.abstention_rate < ABSTENTION_FLOOR
    ):
        failures.append(
            f"abstention_rate={report.abstention_rate} below "
            f"{ABSTENTION_FLOOR} — the gate is not refusing "
            "unanswerable questions"
        )
    if report.false_refusal_rate > FALSE_REFUSAL_CEILING:
        failures.append(
            f"false_refusal_rate={report.false_refusal_rate} above "
            f"{FALSE_REFUSAL_CEILING} — the gate refuses answerable "
            "questions"
        )
    if report.citation_rate < CITATION_FLOOR:
        failures.append(
            f"citation_rate={report.citation_rate} below {CITATION_FLOOR}"
        )

    baseline = load_answer_baseline(
        report.llm_model, golden_set=EVAL_GOLDEN_SET, profile=EVAL_PROFILE
    )
    if baseline is not None:
        for metric in ("abstention_rate", "citation_rate"):
            floor = baseline.get(metric)
            value = getattr(report, metric)
            if floor is not None and value is not None and value < floor - 0.05:
                failures.append(
                    f"{metric}={value} regressed below baseline {floor}"
                )
        ceiling = baseline.get("false_refusal_rate")
        if (
            ceiling is not None
            and report.false_refusal_rate > ceiling + 0.05
        ):
            failures.append(
                f"false_refusal_rate={report.false_refusal_rate} above "
                f"baseline {ceiling}"
            )

    assert not failures, f"{failures}; full report: {artifact}"
    if baseline is None:
        pytest.skip(
            f"behaviour floors passed; no committed baseline for "
            f"({report.llm_model!r}, {EVAL_GOLDEN_SET}, {EVAL_PROFILE}) "
            f"yet — review {artifact} and commit to "
            f"{answer_baseline_path(report.llm_model, golden_set=EVAL_GOLDEN_SET, profile=EVAL_PROFILE)}"
        )
