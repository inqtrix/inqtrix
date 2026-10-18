"""Sufficiency-gate tests: parsing, second pass, honest refusal.

A scripted LLM distinguishes gate prompts (strict-JSON instruction)
from answer prompts, so every flow is deterministic and offline:
sufficient -> answer; insufficient + rewrite -> second retrieval pass;
persistently insufficient -> the honest no-evidence answer with empty
references; gate off -> the legacy single-call path.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.gate import (
    GATE_MARKER_FALLBACK,
    GATE_MARKER_PARSED,
    evaluate_evidence,
)
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    DocumentChunk,
    KnowledgeProviderContext,
    RetrievalCandidate,
)
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import AgentSettings, Settings

from tests.contract._app import StubSearch
from tests.test_knowledge_engine import StubEmbeddings


class ScriptedLLM:
    """Returns queued gate verdicts; answer prompts get a fixed reply.

    Gate prompts are recognized by the strict-JSON instruction from
    the gate template.
    """

    def __init__(self, gate_verdicts: list[dict[str, Any] | str]) -> None:
        self._gate_verdicts = list(gate_verdicts)
        self.gate_prompts: list[str] = []
        self.answer_prompts: list[str] = []

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return "ok"

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if "EXCLUSIVELY with a JSON object" in prompt:
            self.gate_prompts.append(prompt)
            verdict = self._gate_verdicts.pop(0)
            content = (
                verdict if isinstance(verdict, str) else json.dumps(verdict)
            )
            return LLMResponse(
                content=content,
                prompt_tokens=5,
                completion_tokens=3,
                model="stub-gate",
                finish_reason="stop",
            )
        self.answer_prompts.append(prompt)
        return LLMResponse(
            content=(
                "QUOTES:\n"
                '[K1] "Die Haftung ist auf den Auftragswert begrenzt."\n'
                "ANSWER:\n"
                "Antwort mit Beleg [K1]."
            ),
            prompt_tokens=42,
            completion_tokens=11,
            model="stub-answer",
            finish_reason="stop",
        )

    def is_available(self) -> bool:
        return True


class RecordingStore(MemoryKnowledgeStore):
    """Memory store recording every search invocation's context.

    ``grow=True`` surfaces a fresh candidate per search so a rewrite round adds
    new evidence (exercising the second-pass re-gate); ``grow=False`` keeps the
    fixed corpus (a rewrite finds nothing new → the no-new-evidence early-stop).
    """

    def __init__(self, *, grow: bool = False) -> None:
        super().__init__()
        self.search_calls = 0
        self._grow = grow

    async def search(self, **kwargs):
        self.search_calls += 1
        results = list(await super().search(**kwargs))
        if self._grow:
            results.append(
                RetrievalCandidate(
                    chunk=DocumentChunk(
                        id=f"grow-{self.search_calls}",
                        document_id=f"kd_grow_{self.search_calls}",
                        collection_id="kc_grow",
                        chunk_index=0,
                        text=f"Zusatzbeleg {self.search_calls}",
                        source_text=f"Zusatzbeleg {self.search_calls}",
                        source_start=0,
                        source_end=len(f"Zusatzbeleg {self.search_calls}"),
                        document_content_hash=(
                            f"sha256:grow-{self.search_calls}"
                        ),
                        source_verified=True,
                    ),
                    score=0.05,
                    document_title=f"Zusatz {self.search_calls}",
                )
            )
        return results


def make_algorithm(
    llm: ScriptedLLM, *, gate_enabled: bool = True, grow: bool = False
) -> tuple[KnowledgeAlgorithm, RecordingStore, RunContext, RuntimeContext]:
    store = RecordingStore(grow=grow)
    knowledge = KnowledgeProviderContext(
        embeddings=StubEmbeddings(), store=store, default_top_k=4
    )
    service = KnowledgeService(
        knowledge=knowledge, chunk_max_chars=2_000, max_document_chars=100_000
    )

    async def _seed() -> None:
        collection = await service.create_collection(name="K")
        await service.add_document(
            collection_id=collection.id,
            title="Rahmenvertrag",
            text="Die Haftung ist auf den Auftragswert begrenzt.",
        )

    asyncio.run(_seed())
    algorithm = KnowledgeAlgorithm(
        knowledge=knowledge, gate_enabled=gate_enabled
    )
    settings = Settings(agent=AgentSettings())
    runtime = RuntimeContext(
        settings=settings,
        registry=None,
        providers=ProviderContext(llm=llm, search=StubSearch()),
        strategies=None,
    )
    context = RunContext(
        providers=runtime.providers,
        strategies=None,
        agent_settings=settings.agent,
    )
    return algorithm, store, context, runtime


def run_question(algorithm, runtime, context, question="Wie ist die Haftung?"):
    return algorithm.run(
        RunRequest(mode="knowledge", question=question),
        runtime=runtime,
        context=context,
    )


# ------------------------------------------------------------------ #
# evaluate_evidence parsing
# ------------------------------------------------------------------ #


class OneShotLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=self._content,
            prompt_tokens=5,
            completion_tokens=3,
            model="m",
            finish_reason="stop",
        )


@pytest.mark.parametrize(
    ("content", "sufficient", "rewritten", "marker"),
    [
        (
            '{"sufficient": true, "rewritten_query": null, "reason": "ok"}',
            True,
            None,
            GATE_MARKER_PARSED,
        ),
        (
            'Hier: {"sufficient": false, "rewritten_query": '
            '"Haftungsbegrenzung Vertrag", "reason": "zu duenn"}',
            False,
            "Haftungsbegrenzung Vertrag",
            GATE_MARKER_PARSED,
        ),
        ("voellig unparsebarer Text", True, None, GATE_MARKER_FALLBACK),
        ('{"kaputt": 1}', True, None, GATE_MARKER_FALLBACK),
    ],
)
def test_evaluate_evidence_parsing(content, sufficient, rewritten, marker):
    decision, usage = evaluate_evidence(
        OneShotLLM(content),
        question="?",
        evidence_block="[K1] x",
        model=None,
        timeout=30,
    )
    assert decision.sufficient is sufficient
    assert decision.rewritten_query == rewritten
    assert decision.marker == marker
    assert usage == {"prompt_tokens": 5, "completion_tokens": 3}


# ------------------------------------------------------------------ #
# Algorithm flows
# ------------------------------------------------------------------ #


def test_sufficient_evidence_answers_normally():
    llm = ScriptedLLM([{"sufficient": True, "rewritten_query": None}])
    algorithm, store, context, runtime = make_algorithm(llm)

    result = run_question(algorithm, runtime, context)

    assert result.answer.startswith("Antwort mit Beleg")
    assert len(llm.gate_prompts) == 1
    assert len(llm.answer_prompts) == 1
    assert store.search_calls == 1
    state = result.raw["result_state"]
    assert state["knowledge_gate"]["sufficient"] is True
    assert state["knowledge_gate"]["marker"] == GATE_MARKER_PARSED
    # Gate tokens are accounted in the run usage.
    assert result.raw["usage"]["prompt_tokens"] == 42 + 5


def test_insufficient_with_rewrite_runs_one_second_pass():
    llm = ScriptedLLM(
        [
            {
                "sufficient": False,
                "rewritten_query": "Haftungsbegrenzung Auftragswert",
            },
            {"sufficient": True, "rewritten_query": None},
        ]
    )
    # grow=True so the rewrite surfaces new evidence → the second pass actually
    # runs (without new evidence the no-new-evidence early-stop would skip it).
    algorithm, store, context, runtime = make_algorithm(llm, grow=True)

    result = run_question(algorithm, runtime, context)

    assert result.answer.startswith("Antwort mit Beleg")
    assert len(llm.gate_prompts) == 2
    assert store.search_calls == 2
    state = result.raw["result_state"]
    assert state["queries"] == [
        "Wie ist die Haftung?",
        "Haftungsbegrenzung Auftragswert",
    ]
    assert state["knowledge_gate"]["second_pass"] is True
    # Both gate calls billed.
    assert result.raw["usage"]["prompt_tokens"] == 42 + 10


def test_persistently_insufficient_yields_honest_no_evidence_answer():
    llm = ScriptedLLM(
        [
            {"sufficient": False, "rewritten_query": "alternative Suche"},
            {"sufficient": False, "rewritten_query": None},
        ]
    )
    algorithm, _store, context, runtime = make_algorithm(llm)

    result = run_question(algorithm, runtime, context)

    assert "keine relevanten" in result.answer
    # No answer-LLM call was made; no references are fabricated.
    assert llm.answer_prompts == []
    assert result.raw["result_state"]["report_references"] == []
    assert result.raw["result_state"]["knowledge_gate"]["sufficient"] is False


def test_insufficient_without_rewrite_refuses_immediately():
    llm = ScriptedLLM([{"sufficient": False, "rewritten_query": None}])
    algorithm, store, context, runtime = make_algorithm(llm)

    result = run_question(algorithm, runtime, context)

    assert "keine relevanten" in result.answer
    assert store.search_calls == 1
    assert llm.answer_prompts == []


def test_gate_off_restores_single_call_path():
    llm = ScriptedLLM([])
    algorithm, store, context, runtime = make_algorithm(
        llm, gate_enabled=False
    )

    result = run_question(algorithm, runtime, context)

    assert result.answer.startswith("Antwort mit Beleg")
    assert llm.gate_prompts == []
    assert store.search_calls == 1
    assert result.raw["result_state"]["knowledge_gate"] == {"enabled": False}
    assert result.raw["usage"]["prompt_tokens"] == 42


# ------------------------------------------------------------------ #
# Three-way coverage verdict (live-eval finding: partial != none)
# ------------------------------------------------------------------ #


def test_partial_coverage_answers_with_gaps_instead_of_refusing():
    """Insufficient + partial coverage must ANSWER (the answer prompt
    names the gaps) — the blanket refusal is reserved for irrelevant
    evidence. Pinned against the measured DORA false-refusal class."""
    llm = ScriptedLLM([
        {
            "sufficient": False,
            "coverage": "partial",
            "rewritten_query": None,
            "reason": "ein Aspekt fehlt",
        },
    ])
    algorithm, _store, context, runtime = make_algorithm(llm)
    result = run_question(algorithm, runtime, context)
    assert "keine relevanten Inhalte" not in result.answer
    state = result.raw["result_state"]
    assert state["knowledge_gate"]["coverage"] == "partial"
    assert state["knowledge_evidence_used"] >= 1


def test_none_coverage_still_refuses_honestly():
    llm = ScriptedLLM([
        {
            "sufficient": False,
            "coverage": "none",
            "rewritten_query": None,
            "reason": "themenfremd",
        },
    ])
    algorithm, _store, context, runtime = make_algorithm(llm)
    result = run_question(algorithm, runtime, context)
    assert "keine relevanten Inhalte" in result.answer


def test_legacy_verdict_without_coverage_maps_conservatively():
    """Old-style binary responses keep their semantics: insufficient
    without a coverage field still refuses (maps to none)."""
    llm = ScriptedLLM([
        {"sufficient": False, "rewritten_query": None, "reason": "zu duenn"},
    ])
    algorithm, _store, context, runtime = make_algorithm(llm)
    result = run_question(algorithm, runtime, context)
    assert "keine relevanten Inhalte" in result.answer
