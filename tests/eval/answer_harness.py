"""Answer-side evaluation: abstention honesty, refusals, citations.

Runs every golden query through the REAL ``KnowledgeAlgorithm`` (gate
included) and grades the behaviour the retrieval metrics cannot see:

* abstention rate — the ``no_evidence`` queries must end in the honest
  canonical no-evidence answer (the gate's whole purpose).
* false-refusal rate — answerable queries must NOT be refused; an
  over-eager gate destroys utility silently.
* citation rate — answered queries must carry ``[K#]`` markers.
* gate telemetry — second-pass and parse-fallback rates make the
  gate's live behaviour visible instead of assumed.

This harness has no claim-level faithfulness judge. Any judge model
for answer-side metrics must be PINNED, because a judge swap silently
re-baselines every threshold.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


from tests.eval.harness import (
    ARTIFACTS_DIR,
    BASELINES_DIR,
    GOLDEN_DIR,
    GoldenQuery,
    load_corpus,
    load_queries,
)

REFUSAL_MARKER = "keine relevanten"
_CITATION = re.compile(r"\[K\d+\]")

_ABSENCE_VERBS = (
    r"(?:ersichtlich|hervor(?:geht)?|entnehmen|enthalten|enthält|"
    r"genannt|nennen|nennt|beziffert|beziffern|angegeben|ableiten|"
    r"beantworten|aufgeführt|aufgefuehrt|finden|liefern|liefert|"
    r"lässt|laesst|geht|gibt)"
)
_NEGATION = r"(?:nicht|kein\w*)"
_FILLER = r"(?:[\w().,§/-]+\s+){0,5}"
_DECLARED_ABSENT = re.compile(
    rf"{_NEGATION}\s+{_FILLER}{_ABSENCE_VERBS}"
    rf"|{_ABSENCE_VERBS}\s+{_FILLER}{_NEGATION}"
    r"|keine\s+angabe",
    re.IGNORECASE,
)
"""Honest-absence phrasings, the abstention ruler's second half.

Since the coverage verdict, the gate answers PARTIAL evidence instead
of refusing wholesale; unanswerable questions then end in answers
that explicitly declare the asked fact absent ("ist aus den Auszuegen
nicht ersichtlich", "die Auszuege nennen keine Geldbusse") rather
than in the canonical refusal sentence. Those ARE honest abstentions
— a ruler that only greps the canonical sentence misreports them as
hallucination risk. Both word orders are covered (negation before or
after the absence verb); markdown emphasis is stripped before
matching. Deliberately a small, documented pattern family; per-query
answer previews in the artifact keep it reviewable.
"""


def _declares_absent(answer: str) -> bool:
    """True when the answer states the asked information is missing."""
    return bool(_DECLARED_ABSENT.search(answer.replace("*", "")))


@dataclass
class AnswerRecord:
    """Outcome of one golden query through the full algorithm.

    ``refused`` (the canonical no-evidence sentence) feeds the
    false-refusal metric for answerable queries; ``declined``
    (refused OR declared-absent) feeds the abstention metric for
    ``no_evidence`` queries. Two flags on purpose: a partial answer
    to an answerable multi-aspect question may legitimately declare
    ONE aspect absent — counting that as a refusal would poison the
    false-refusal metric.
    """

    query: GoldenQuery
    answer: str
    refused: bool
    declined: bool
    cited: bool
    gate_marker: str
    second_pass: bool


@dataclass
class AnswerReport:
    """Aggregated answer-side metrics plus per-query detail.

    ``abstention_rate`` is ``None`` for tiers without ``no_evidence``
    queries (bsi/dora) — there is nothing to abstain from, and a
    fabricated 0.0 would read as a catastrophic regression against a
    base-set baseline.
    """

    llm_model: str
    embedding_model: str
    golden_set: str
    profile: str
    query_count: int
    abstention_rate: float | None
    false_refusal_rate: float
    citation_rate: float
    second_pass_rate: float
    gate_fallback_rate: float
    per_query: list[dict[str, Any]] = field(repr=False, default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "llm_model": self.llm_model,
            "embedding_model": self.embedding_model,
            "golden_set": self.golden_set,
            "profile": self.profile,
            "query_count": self.query_count,
            "abstention_rate": self.abstention_rate,
            "false_refusal_rate": self.false_refusal_rate,
            "citation_rate": self.citation_rate,
            "second_pass_rate": self.second_pass_rate,
            "gate_fallback_rate": self.gate_fallback_rate,
            "per_query": self.per_query,
        }


def ingest_golden_corpus(
    service,
    *,
    golden_dir: Path = GOLDEN_DIR,
    corpus_dir: Path | None = None,
) -> str:
    """Ingest the tier's corpus into a fresh collection; returns its id."""

    async def _ingest() -> str:
        collection = await service.create_collection(name="answer-eval-golden")
        for _doc_id, title, text in load_corpus(
            golden_dir, corpus_dir=corpus_dir
        ):
            await service.add_document(
                collection_id=collection.id, title=title, text=text
            )
        return collection.id

    return asyncio.run(_ingest())


def run_answer_eval(
    *,
    run_algorithm: Callable[[str, str], Any],
    collection_id: str,
    llm_model: str,
    embedding_model: str,
    golden_set: str = "base",
    golden_dir: Path = GOLDEN_DIR,
    corpus_dir: Path | None = None,
    profile: str = "standard",
) -> AnswerReport:
    """Grade every golden query of one tier through *run_algorithm*.

    Args:
        run_algorithm: ``(question, collection_id) -> AgentResult`` —
            the caller owns algorithm construction (providers, gate
            flag, the request's profile) so the harness stays
            backend-agnostic.
        collection_id: The ingested golden collection.
        llm_model: Answer/gate model id for the report header.
        embedding_model: Embedding model id for the report header.
        golden_set: Tier name for the report header and baseline key.
        golden_dir: The tier directory carrying ``queries.json``.
        corpus_dir: Explicit corpus location for corpus-sharing tiers.
        profile: Retrieval-profile name for the report header and
            baseline key (the caller sends it in the request).
    """
    records: list[AnswerRecord] = []
    for query in load_queries(golden_dir, corpus_dir=corpus_dir):
        result = run_algorithm(query.query, collection_id)
        answer = result.answer or ""
        state = result.raw.get("result_state", {})
        gate_state = state.get("knowledge_gate", {})
        refused = REFUSAL_MARKER in answer
        records.append(
            AnswerRecord(
                query=query,
                answer=answer,
                refused=refused,
                declined=refused or _declares_absent(answer),
                cited=bool(_CITATION.search(answer)),
                gate_marker=str(gate_state.get("marker", "")),
                second_pass=bool(gate_state.get("second_pass", False)),
            )
        )

    no_evidence = [r for r in records if r.query.category == "no_evidence"]
    answerable = [r for r in records if r.query.category != "no_evidence"]
    answered = [r for r in answerable if not r.refused]
    return AnswerReport(
        llm_model=llm_model,
        embedding_model=embedding_model,
        golden_set=golden_set,
        profile=profile,
        query_count=len(records),
        abstention_rate=round(
            sum(r.declined for r in no_evidence) / len(no_evidence), 4
        )
        if no_evidence
        else None,
        false_refusal_rate=round(
            sum(r.refused for r in answerable) / len(answerable), 4
        ),
        citation_rate=round(
            sum(r.cited for r in answered) / len(answered), 4
        )
        if answered
        else 0.0,
        second_pass_rate=round(
            sum(r.second_pass for r in records) / len(records), 4
        ),
        gate_fallback_rate=round(
            sum(
                r.gate_marker == "_knowledge_gate_fallback" for r in records
            )
            / len(records),
            4,
        ),
        per_query=[
            {
                "id": r.query.id,
                "category": r.query.category,
                "refused": r.refused,
                "declined": r.declined,
                "cited": r.cited,
                "gate_marker": r.gate_marker,
                "second_pass": r.second_pass,
                "answer_preview": r.answer[:160],
            }
            for r in records
        ],
    )


def write_answer_artifact(report: AnswerReport) -> Path:
    """Persist the answer report as a timestamped JSON artifact."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = report.llm_model.replace("/", "__")
    suffix = f"-{report.golden_set}-{report.profile}"
    path = ARTIFACTS_DIR / (
        f"answer-{safe}{suffix}-{time.time_ns() // 1_000_000}.json"
    )
    path.write_text(
        json.dumps(report.to_payload(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def answer_baseline_path(
    llm_model: str, *, golden_set: str = "base", profile: str = "standard"
) -> Path:
    """Answer baselines are keyed (model, tier, profile).

    The legacy unkeyed name stays valid for the established base/
    standard combination so the existing committed baseline keeps
    gating without a rename commit.
    """
    safe = llm_model.replace("/", "__")
    if golden_set == "base" and profile == "standard":
        return BASELINES_DIR / f"answer-{safe}.json"
    return BASELINES_DIR / f"answer-{safe}-{golden_set}-{profile}.json"


def load_answer_baseline(
    llm_model: str, *, golden_set: str = "base", profile: str = "standard"
) -> dict[str, float] | None:
    """Committed answer-metric floor for one (model, tier, profile)."""
    path = answer_baseline_path(
        llm_model, golden_set=golden_set, profile=profile
    )
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
