"""Profile behaviour of the knowledge algorithm, fully offline.

Pins the profile contracts: exact LLM call counts per
profile (`schnell` = one answer call, nothing else), the capped
rewrite loop, ceiling degradation visible in the result state, the
vocabulary-bridge prompt variant, per-profile rerank wiring, and —
because the algorithm instance is a shared singleton — that two
concurrent runs with different profiles do not interfere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from typing import Any

import pytest

from inqtrix.auth.principal import Principal
from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.exceptions import AgentCancelled
from inqtrix.execution_failures import RunExecutionFailure
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.profiles import EVIDENCE_K_MAX
from inqtrix.knowledge.retrieval_warnings import (
    project_retrieval_exclusion_warnings,
)
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    DocumentChunk,
    KnowledgeProviderContext,
    RetrievalCandidate,
    RetrievalCandidateBatch,
    RetrievalDegradation,
    RetrievalExclusion,
)
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.providers.rerankers import RerankResult
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import AgentSettings, Settings

from tests.contract._app import StubSearch
from tests.test_knowledge_engine import StubEmbeddings


class ScriptedLLM:
    """Queued gate verdicts; answer prompts get a fixed cited reply."""

    def __init__(self, gate_verdicts: list[dict[str, Any]] | None = None) -> None:
        self._gate_verdicts = list(gate_verdicts or [])
        self.gate_prompts: list[str] = []
        self.answer_prompts: list[str] = []

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if "You are a retrieval checker" in prompt:
            self.gate_prompts.append(prompt)
            verdict = self._gate_verdicts.pop(0)
            return LLMResponse(
                content=json.dumps(verdict),
                prompt_tokens=5,
                completion_tokens=3,
                model="stub-gate",
                finish_reason="stop",
            )
        self.answer_prompts.append(prompt)
        return LLMResponse(
            content="Antwort mit Beleg [K1].",
            prompt_tokens=42,
            completion_tokens=11,
            model="stub-answer",
            finish_reason="stop",
        )

    def is_available(self) -> bool:
        return True


class ContextualizingLLM(ScriptedLLM):
    """Scripted follow-up contextualizer plus the normal answer/gate script."""

    def __init__(
        self,
        contextualized_question: str | Exception,
        gate_verdicts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(gate_verdicts)
        self._contextualized_question = contextualized_question
        self.context_prompts: list[str] = []

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if "knowledge RAG system" in prompt and "self-contained search question" in prompt:
            self.context_prompts.append(prompt)
            if isinstance(self._contextualized_question, Exception):
                raise self._contextualized_question
            return LLMResponse(
                content=json.dumps({"question": self._contextualized_question}),
                prompt_tokens=7,
                completion_tokens=4,
                model="stub-context",
                finish_reason="stop",
            )
        return super().complete_with_metadata(prompt, **kwargs)


class DecomposingLLM(ScriptedLLM):
    """Return two real sub-queries before continuing with gate/answer calls."""

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if "self-contained sub-questions" in prompt:
            return LLMResponse(
                content=json.dumps(
                    ["Wie ist die Haftung begrenzt?", "Was ist der Auftragswert?"]
                ),
                prompt_tokens=7,
                completion_tokens=5,
                model="stub-decompose",
                finish_reason="stop",
            )
        return super().complete_with_metadata(prompt, **kwargs)


class RecordingStore(MemoryKnowledgeStore):
    """Memory store recording the top_k of every search call.

    With ``grow=True`` each search also surfaces one FRESH candidate, so a
    rewrite round adds new evidence and the rewrite loop runs to its round
    budget (the multi-round tests). With ``grow=False`` (the small fixed
    corpus) a rewrite surfaces nothing new — exercising the no-new-evidence
    early-stop.
    """

    def __init__(self, *, grow: bool = False) -> None:
        super().__init__()
        self.embedded_queries: list[str] = []
        self.search_top_ks: list[int] = []
        self._grow = grow
        self._calls = 0

    async def search(self, **kwargs):
        self.search_top_ks.append(kwargs["top_k"])
        results = list(await super().search(**kwargs))
        if self._grow:
            self._calls += 1
            results.append(
                RetrievalCandidate(
                    chunk=DocumentChunk(
                        id=f"grow-{self._calls}",
                        document_id=f"kd_grow_{self._calls}",
                        collection_id="kc_grow",
                        chunk_index=0,
                        text=f"Zusatzbeleg {self._calls}",
                        source_text=f"Zusatzbeleg {self._calls}",
                        source_start=0,
                        source_end=len(f"Zusatzbeleg {self._calls}"),
                        document_content_hash=f"sha256:grow-{self._calls}",
                        source_verified=True,
                    ),
                    score=0.05,
                    document_title=f"Zusatz {self._calls}",
                )
            )
        return results


class RecordingReranker:
    """Identity reranker recording invocations."""

    default_model = "stub-rerank"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def rerank(self, query, documents, *, top_n, model=None):
        self.calls.append(len(documents))
        return [
            RerankResult(index=index, relevance_score=1.0 - index * 0.01)
            for index in range(min(top_n, len(documents)))
        ]


class RecordingEmbeddings(StubEmbeddings):
    """Embedding stub recording the query texts passed to retrieval."""

    def __init__(self) -> None:
        super().__init__()
        self.query_texts: list[str] = []

    def embed_query(self, text, *, model=None):
        self.query_texts.append(text)
        return super().embed_query(text, model=model)


def make_algorithm(
    llm: ScriptedLLM,
    *,
    gate_enabled: bool = True,
    grounding_enabled: bool = False,
    gate_max_rounds: int = 3,
    reranker: RecordingReranker | None = None,
    grow: bool = False,
    default_top_k: int = 4,
):
    store = RecordingStore(grow=grow)
    embeddings = RecordingEmbeddings()
    store.embedded_queries = embeddings.query_texts
    knowledge = KnowledgeProviderContext(
        embeddings=embeddings,
        store=store,
        default_top_k=default_top_k,
        reranker=reranker,
        rerank_candidate_depth=10,
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
        # A second document keeps the rerank stage reachable (the
        # pipeline skips reranking single-candidate result sets).
        await service.add_document(
            collection_id=collection.id,
            title="Anlage Vergueetung",
            text="Die Verguetung richtet sich nach dem Auftragswert.",
        )

    asyncio.run(_seed())
    algorithm = KnowledgeAlgorithm(
        knowledge=knowledge,
        gate_enabled=gate_enabled,
        grounding_enabled=grounding_enabled,
        gate_max_rounds=gate_max_rounds,
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


def run_with_profile(algorithm, runtime, context, profile=None, events=None):
    filters: dict[str, Any] = {}
    if profile is not None:
        filters["profile"] = profile
    if events is not None:
        context = RunContext(
            providers=context.providers,
            strategies=None,
            agent_settings=context.agent_settings,
            event_sink=lambda event, payload: events.append((event, payload)),
        )
    return algorithm.run(
        RunRequest(
            mode="knowledge",
            question="Wie ist die Haftung geregelt?",
            knowledge_filters=filters,
        ),
        runtime=runtime,
        context=context,
    )


def run_followup(
    algorithm: KnowledgeAlgorithm,
    runtime: RuntimeContext,
    context: RunContext,
    *,
    events: list[tuple[str, dict[str, Any]]] | None = None,
) -> Any:
    """Run a follow-up Knowledge request with prior Q&A history."""
    if events is not None:
        context = RunContext(
            providers=context.providers,
            strategies=None,
            agent_settings=context.agent_settings,
            event_sink=lambda event, payload: events.append((event, payload)),
        )
    return algorithm.run(
        RunRequest(
            mode="knowledge",
            question="Und die Haftung?",
            history=(
                "Nutzer: Wie ist der Rahmenvertrag geregelt?\n"
                "Assistent: Der Rahmenvertrag nennt den Auftragswert."
            ),
            knowledge_filters={"profile": "schnell"},
        ),
        runtime=runtime,
        context=context,
    )


def retrieval_queries(store: RecordingStore) -> list[str]:
    """Return real retrieval queries, excluding the vector dimension probe."""
    return [
        query for query in store.embedded_queries
        if query != "dimension probe"
    ]


SUFFICIENT = {"sufficient": True, "rewritten_query": None, "reason": "ok"}
REWRITE = {
    "sufficient": False,
    "rewritten_query": "Haftungsbegrenzung Auftragswert",
    "reason": "zu duenn",
}


class TestCallCounts:
    def test_schnell_makes_exactly_one_llm_call(self):
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        result = run_with_profile(algorithm, runtime, context, "schnell")
        assert llm.gate_prompts == []
        assert len(llm.answer_prompts) == 1
        assert len(store.search_top_ks) == 1
        state = result.raw["result_state"]
        assert state["knowledge_profile"]["id"] == "schnell"
        assert state["knowledge_gate"]["enabled"] is False

    def test_no_profile_runs_the_legacy_standard_flow(self):
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        result = run_with_profile(algorithm, runtime, context, None)
        assert len(llm.gate_prompts) == 1
        assert len(llm.answer_prompts) == 1
        state = result.raw["result_state"]
        assert state["knowledge_profile"]["id"] == "standard"
        assert state["knowledge_profile"]["requested"] is None
        assert state["knowledge_gate"]["rounds_used"] == 0
        assert state["knowledge_gate"]["max_rounds"] == 1
        assert "second_pass" not in state["knowledge_gate"]

    def test_standard_stops_after_one_rewrite_round(self):
        llm = ScriptedLLM([REWRITE, REWRITE])
        algorithm, store, context, runtime = make_algorithm(llm, grow=True, default_top_k=20)
        result = run_with_profile(algorithm, runtime, context, "standard")
        # Initial gate + exactly one re-gate; the second insufficient
        # verdict must NOT trigger a third retrieval.
        assert len(llm.gate_prompts) == 2
        assert len(store.search_top_ks) == 2
        state = result.raw["result_state"]
        assert state["knowledge_gate"]["rounds_used"] == 1
        assert state["knowledge_gate"]["second_pass"] is True
        # Persistently insufficient -> honest refusal.
        assert state["knowledge_evidence_used"] == 0
        assert "keine relevanten Inhalte" in result.answer

    def test_gruendlich_loops_up_to_two_rounds(self):
        llm = ScriptedLLM([REWRITE, REWRITE, SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm, grow=True, default_top_k=20)
        result = run_with_profile(algorithm, runtime, context, "gruendlich")
        assert len(llm.gate_prompts) == 3
        assert len(store.search_top_ks) == 3
        state = result.raw["result_state"]
        assert state["knowledge_gate"]["rounds_used"] == 2
        assert state["knowledge_gate"]["sufficient"] is True
        assert len(state["queries"]) == 3

    def test_gate_stops_early_when_a_rewrite_adds_no_new_evidence(self):
        # Gruendlich allows two rewrite rounds, but the small seeded corpus is
        # exhausted after the first rewrite returns nothing new — the loop must
        # stop without spending a re-gate call, and say so (no silent spin).
        llm = ScriptedLLM([REWRITE, REWRITE, REWRITE])
        events: list[tuple[str, dict]] = []
        algorithm, _store, context, runtime = make_algorithm(llm)
        result = run_with_profile(
            algorithm, runtime, context, "gruendlich", events=events
        )
        state = result.raw["result_state"]
        assert state["knowledge_gate"]["exhausted"] is True
        assert state["knowledge_gate"]["rounds_used"] == 1
        # Only the INITIAL gate ran; the no-new-evidence round skipped its re-gate.
        assert len(llm.gate_prompts) == 1
        assert any(
            event == "inqtrix.knowledge.gate.exhausted" for event, _ in events
        )

    def test_tief_round_budget_tracks_the_ceiling_cap(self):
        llm = ScriptedLLM([REWRITE, REWRITE, REWRITE, SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm, gate_max_rounds=3, grow=True, default_top_k=20)
        result = run_with_profile(algorithm, runtime, context, "tief")
        state = result.raw["result_state"]
        assert state["knowledge_gate"]["rounds_used"] == 3
        assert state["knowledge_gate"]["max_rounds"] == 3


class TestVocabularyBridge:
    def test_gruendlich_uses_the_bridge_prompt(self):
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "gruendlich")
        assert "technical language of the documents" in llm.gate_prompts[0]

    def test_standard_keeps_the_pre_profile_prompt(self):
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "standard")
        assert "technical language of the documents" not in llm.gate_prompts[0]
        assert "other terms, synonyms" in llm.gate_prompts[0]


class TestRerankWiring:
    def test_schnell_skips_a_wired_reranker(self):
        reranker = RecordingReranker()
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(
            llm, reranker=reranker
        )
        run_with_profile(algorithm, runtime, context, "schnell")
        assert reranker.calls == []
        # Without the rerank stage the store is queried at plain top_k.
        assert store.search_top_ks == [4]

    def test_gruendlich_scales_the_candidate_depth(self):
        reranker = RecordingReranker()
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(
            llm, reranker=reranker
        )
        run_with_profile(algorithm, runtime, context, "gruendlich")
        # Configured depth 10 x 1.5 = 15.
        assert store.search_top_ks == [15]
        assert len(reranker.calls) == 1


class TestCeilingDegradation:
    def test_gate_off_env_degrades_gruendlich_visibly(self):
        llm = ScriptedLLM()
        algorithm, _store, context, runtime = make_algorithm(
            llm, gate_enabled=False
        )
        result = run_with_profile(algorithm, runtime, context, "gruendlich")
        assert llm.gate_prompts == []
        state = result.raw["result_state"]
        assert state["knowledge_gate"]["enabled"] is False
        assert "gate" in state["knowledge_profile"]["degraded_stages"]

    def test_invalid_profile_fails_loudly(self):
        llm = ScriptedLLM()
        algorithm, _store, context, runtime = make_algorithm(llm)
        with pytest.raises(ValueError, match="schnell"):
            run_with_profile(algorithm, runtime, context, "turbo")


class TestConversationalContext:
    def test_followup_history_rewrites_the_retrieval_query(self):
        events: list[tuple[str, dict]] = []
        llm = ContextualizingLLM(
            "Wie ist die Haftung im Rahmenvertrag geregelt?"
        )
        algorithm, store, context, runtime = make_algorithm(llm)
        result = run_followup(algorithm, runtime, context, events=events)

        queries = retrieval_queries(store)
        assert queries[0] == (
            "Wie ist die Haftung im Rahmenvertrag geregelt?"
        )
        assert llm.context_prompts, "history must trigger one context call"
        assert "Und die Haftung?" in llm.answer_prompts[0]
        assert "Conversation so far:" in llm.answer_prompts[0]
        contextualized = dict(events)["inqtrix.knowledge.contextualized"]
        assert contextualized == {
            "marker": "_knowledge_query_context_applied",
            "rewritten": True,
            "used_history": True,
        }
        state = result.raw["result_state"]
        assert state["knowledge_contextualization"] == contextualized
        assert state["queries"][0] == queries[0]

    def test_contextualization_failure_falls_back_loudly(self, caplog):
        events: list[tuple[str, dict]] = []
        llm = ContextualizingLLM(RuntimeError("context down"))
        algorithm, store, context, runtime = make_algorithm(llm)

        with caplog.at_level(logging.WARNING, logger="inqtrix"):
            result = run_followup(algorithm, runtime, context, events=events)

        assert retrieval_queries(store)[0] == "Und die Haftung?"
        assert any("_knowledge_query_context_fallback" in message for message in caplog.messages)
        contextualized = dict(events)["inqtrix.knowledge.contextualized"]
        assert contextualized == {
            "marker": "_knowledge_query_context_fallback",
            "rewritten": False,
            "used_history": True,
        }
        assert result.raw["result_state"]["knowledge_contextualization"] == contextualized


class TestProfileEvent:
    def test_empty_pinned_scope_skips_models_embeddings_and_store_search(self):
        events: list[tuple[str, dict[str, Any]]] = []
        llm = ContextualizingLLM("Das darf nicht aufgerufen werden")
        algorithm, store, context, runtime = make_algorithm(llm)
        scoped_context = RunContext(
            providers=context.providers,
            strategies=context.strategies,
            agent_settings=context.agent_settings,
            principal=Principal(
                user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
                kind="oidc_session",
                role="member",
            ),
            event_sink=lambda event, payload: events.append((event, payload)),
        )

        result = algorithm.run(
            RunRequest(
                mode="knowledge",
                question="Und die Haftung?",
                history="Nutzer: Was steht im Vertrag?",
                knowledge_filters={
                    "profile": "gruendlich",
                    "collection_ids": [],
                },
            ),
            runtime=runtime,
            context=scoped_context,
        )

        assert store.search_top_ks == []
        assert retrieval_queries(store) == []
        assert llm.context_prompts == []
        assert llm.gate_prompts == []
        assert llm.answer_prompts == []
        assert result.raw["result_state"]["knowledge_collections"] == []
        assert dict(events)["inqtrix.knowledge.scope.empty"] == {
            "scope": "empty",
            "collection_count": 0,
        }
        assert dict(events)["inqtrix.knowledge.retrieval.completed"][
            "collection_document_count"
        ] == 0

    def test_authenticated_run_without_pinned_scope_fails_before_retrieval(self):
        events: list[tuple[str, dict[str, Any]]] = []
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        scoped_context = RunContext(
            providers=context.providers,
            strategies=context.strategies,
            agent_settings=context.agent_settings,
            principal=Principal(
                user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
                kind="oidc_session",
                role="member",
            ),
            event_sink=lambda event, payload: events.append((event, payload)),
        )

        with pytest.raises(RunExecutionFailure) as raised:
            algorithm.run(
                RunRequest(
                    mode="knowledge",
                    question="Wie ist die Haftung geregelt?",
                    knowledge_filters={"profile": "schnell"},
                ),
                runtime=runtime,
                context=scoped_context,
            )

        assert raised.value.error_type == "knowledge_scope_missing"
        assert store.search_top_ks == []
        assert events[-1] == (
            "inqtrix.knowledge.scope.unscoped_principal",
            {"marker": "_knowledge_unscoped_principal", "scope": "blocked"},
        )

    def test_profile_resolved_event_carries_the_plan(self):
        events: list[tuple[str, dict]] = []
        llm = ScriptedLLM()
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(
            algorithm, runtime, context, "auto", events=events
        )
        payloads = dict(events)
        resolved = payloads["inqtrix.knowledge.profile.resolved"]
        assert resolved["requested_profile"] == "auto"
        assert resolved["auto_selected"] is True
        assert resolved["profile"] in ("schnell", "standard", "gruendlich")
        assert resolved["auto_reason"]

    def test_gate_events_carry_the_round_index(self):
        events: list[tuple[str, dict]] = []
        llm = ScriptedLLM([REWRITE, SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm, grow=True, default_top_k=20)
        run_with_profile(
            algorithm, runtime, context, "standard", events=events
        )
        rounds = [
            payload["round"]
            for event, payload in events
            if event == "inqtrix.knowledge.gate.evaluated"
        ]
        assert rounds == [0, 1]


class TestEvidenceBreadth:
    def test_retrieval_event_carries_final_k(self):
        """The retrieval event exposes both the per-query width (top_k) and
        the profile's wider final evidence budget (final_k) — visibility."""
        events: list[tuple[str, dict]] = []
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "standard", events=events)
        retrieval = dict(events)["inqtrix.knowledge.retrieval.completed"]
        # STANDARD keeps final_k == top_k (factor 1.0); default_top_k is 4 here.
        assert retrieval["top_k"] == 4
        assert retrieval["final_k"] == 4
        # Coverage: the harness seeds 2 documents in the single collection, all
        # eligible for retrieval — surfaced so the UI can confirm the scope.
        assert retrieval["collection_document_count"] == 2

    def test_retrieval_degradation_is_emitted_and_persisted(self):
        events: list[tuple[str, dict]] = []
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        original = store.search

        async def degraded_search(**kwargs):
            candidates = await original(**kwargs)
            return RetrievalCandidateBatch(
                candidates,
                degradations=(
                    RetrievalDegradation(
                        reason="vector_overfetch_cap",
                        retrieval_mode="dense",
                        requested_top_k=kwargs["top_k"],
                        returned_hits=len(candidates),
                        candidate_cap=64,
                    ),
                ),
            )

        store.search = degraded_search  # type: ignore[method-assign]
        result = run_with_profile(
            algorithm, runtime, context, "standard", events=events
        )

        degraded = [
            payload
            for event, payload in events
            if event == "inqtrix.knowledge.retrieval.degraded"
        ]
        assert degraded == [
            {
                "reason": "vector_overfetch_cap",
                "retrieval_mode": "dense",
                "stage": "vector_candidate_pool",
                "requested_candidate_pool": 4,
                "returned_candidate_pool": 2,
                "final_top_k": 4,
                "final_evidence_complete": False,
                "requested_top_k": 4,
                "returned_hits": 2,
                "candidate_cap": 64,
            }
        ]
        assert result.raw["result_state"]["knowledge_retrieval"] == {
            "degradations": degraded
        }

    def test_single_query_projects_all_typed_exclusions_to_run_warnings(self):
        events: list[tuple[str, dict[str, Any]]] = []
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        original = store.search

        async def search_with_exclusions(**kwargs):
            candidates = await original(**kwargs)
            return RetrievalCandidateBatch(
                candidates,
                exclusions=(
                    RetrievalExclusion(
                        reason="source_unverified",
                        stage="canonical_hydration",
                        count=2,
                        recommended_action="reindex",
                    ),
                    RetrievalExclusion(
                        reason="canonical_chunk_unavailable",
                        stage="canonical_hydration",
                        count=3,
                        recommended_action="reconcile",
                    ),
                ),
            )

        store.search = search_with_exclusions  # type: ignore[method-assign]
        result = run_with_profile(
            algorithm, runtime, context, "schnell", events=events
        )

        warnings = result.raw["result_state"]["knowledge_retrieval"]["warnings"]
        assert [(warning["code"], warning["count"]) for warning in warnings] == [
            ("chunks_require_reindex", 2),
            ("chunks_pending_reconciliation", 3),
        ]
        completed = dict(events)["inqtrix.knowledge.retrieval.completed"]
        assert completed["warnings"] == warnings
        assert [
            payload
            for event, payload in events
            if event == "inqtrix.knowledge.retrieval.warning"
        ] == warnings
        assert all("text" not in warning for warning in warnings)

    def test_unknown_exclusion_reason_is_not_silently_discarded(self):
        warnings = project_retrieval_exclusion_warnings(
            (
                RetrievalExclusion(
                    reason="future_integrity_rule",
                    stage="canonical_hydration",
                    count=1,
                ),
            )
        )

        assert warnings[0].code == "retrieval_candidates_excluded"
        assert warnings[0].reason == "future_integrity_rule"
        assert warnings[0].count == 1

    def test_decomposition_interleave_accumulates_exclusions_without_loss(self):
        events: list[tuple[str, dict[str, Any]]] = []
        llm = DecomposingLLM([SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        original = store.search
        calls = 0

        async def search_with_exclusions(**kwargs):
            nonlocal calls
            calls += 1
            candidates = await original(**kwargs)
            return RetrievalCandidateBatch(
                candidates,
                exclusions=(
                    RetrievalExclusion(
                        reason="source_unverified",
                        stage="canonical_hydration",
                        count=calls,
                        recommended_action="reindex",
                    ),
                ),
            )

        store.search = search_with_exclusions  # type: ignore[method-assign]
        result = run_with_profile(
            algorithm, runtime, context, "tief", events=events
        )

        assert calls == 3  # original question + two decomposed sub-queries
        warning = result.raw["result_state"]["knowledge_retrieval"]["warnings"][0]
        assert warning == {
            "code": "chunks_require_reindex",
            "reason": "source_unverified",
            "stage": "canonical_hydration",
            "count": 6,
            "recommended_action": "reindex",
        }
        assert dict(events)["inqtrix.knowledge.retrieval.completed"]["warnings"] == [
            warning
        ]

    def test_gate_rewrite_merge_retains_late_exclusions(self):
        events: list[tuple[str, dict[str, Any]]] = []
        llm = ScriptedLLM([REWRITE, SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(
            llm,
            grow=True,
            default_top_k=20,
        )
        original = store.search
        calls = 0

        async def search_with_exclusions(**kwargs):
            nonlocal calls
            calls += 1
            candidates = await original(**kwargs)
            return RetrievalCandidateBatch(
                candidates,
                exclusions=(
                    RetrievalExclusion(
                        reason="canonical_chunk_unavailable",
                        stage="canonical_hydration",
                        count=calls,
                        recommended_action="reconcile",
                    ),
                ),
            )

        store.search = search_with_exclusions  # type: ignore[method-assign]
        result = run_with_profile(
            algorithm, runtime, context, "standard", events=events
        )

        assert calls == 2
        assert dict(events)["inqtrix.knowledge.retrieval.completed"]["warnings"][
            0
        ]["count"] == 1
        final_warning = result.raw["result_state"]["knowledge_retrieval"][
            "warnings"
        ][0]
        assert final_warning["code"] == "chunks_pending_reconciliation"
        assert final_warning["count"] == 3
        assert [
            payload
            for event, payload in events
            if event == "inqtrix.knowledge.retrieval.warning"
        ] == [final_warning]

    def test_deep_widens_final_k_beyond_top_k(self):
        """Deep (TIEF, factor 2.0) raises the final evidence budget above the
        per-query top_k, so its fan-out can surface a broader evidence set."""
        events: list[tuple[str, dict]] = []
        # Decompose + gate both hit the scripted LLM; SUFFICIENT keeps the gate
        # from looping. final_k is computed regardless of decomposition.
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "tief", events=events)
        retrieval = dict(events)["inqtrix.knowledge.retrieval.completed"]
        assert retrieval["top_k"] == 4
        assert retrieval["final_k"] == 8  # 4 * 2.0


class TestCitationProvenance:
    def test_references_carry_the_retrieved_excerpt_and_explicit_ids(self):
        """Each citation ships the exact retrieved chunk + explicit document/
        chunk ids, so the client can show the cited passage and open the source
        reliably (not by parsing the URL)."""
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        result = run_with_profile(algorithm, runtime, context, "standard")
        references = result.raw["result_state"]["report_references"]
        assert references, "a grounded answer must carry references"
        first = references[0]
        assert first["label"] == "K1"
        assert first["document_id"]  # explicit id → reliable open
        assert isinstance(first["chunk_index"], int)
        assert first["excerpt"]  # the exact retrieved passage travels along
        assert "source_text" in first


class TestSingletonConcurrency:
    def test_parallel_runs_with_different_profiles_do_not_interfere(self):
        """The shared instance must keep all per-request state local:
        a `schnell` run and a `standard` run overlap mid-flight and
        each result must reflect its own plan."""
        barrier = threading.Barrier(2, timeout=5)

        class BarrierLLM(ScriptedLLM):
            def complete_with_metadata(self, prompt: str, **kwargs):
                if prompt and "You are a retrieval checker" not in prompt:
                    barrier.wait()
                return super().complete_with_metadata(prompt, **kwargs)

        llm = BarrierLLM([SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        results: dict[str, Any] = {}
        errors: list[BaseException] = []

        def worker(profile: str) -> None:
            try:
                results[profile] = run_with_profile(
                    algorithm, runtime, context, profile
                )
            except BaseException as exc:  # noqa: BLE001 - reraised below
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=("schnell",)),
            threading.Thread(target=worker, args=("standard",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        schnell_state = results["schnell"].raw["result_state"]
        standard_state = results["standard"].raw["result_state"]
        assert schnell_state["knowledge_profile"]["id"] == "schnell"
        assert schnell_state["knowledge_gate"]["enabled"] is False
        assert standard_state["knowledge_profile"]["id"] == "standard"
        assert standard_state["knowledge_gate"]["enabled"] is True


class TestFinalKOverride:
    """An explicit ``final_k`` pins the surfaced-evidence count, overriding the
    profile factor, clamped to the ceiling, and surfaced as overridden."""

    def _run(self, store_algo, filters):
        algorithm, store, context, runtime = store_algo
        events: list[tuple[str, dict[str, Any]]] = []
        ctx = RunContext(
            providers=context.providers,
            strategies=None,
            agent_settings=context.agent_settings,
            event_sink=lambda event, payload: events.append((event, payload)),
        )
        algorithm.run(
            RunRequest(
                mode="knowledge",
                question="Wie ist die Haftung geregelt?",
                knowledge_filters=filters,
            ),
            runtime=runtime,
            context=ctx,
        )
        return store, dict(events)["inqtrix.knowledge.retrieval.completed"]

    def test_explicit_final_k_sets_the_single_retrieval_width(self):
        # `schnell` does not decompose, so the one retrieval runs at final_k.
        store, completed = self._run(
            make_algorithm(ScriptedLLM(), default_top_k=4),
            {"profile": "schnell", "final_k": 12},
        )
        assert store.search_top_ks == [12]
        assert completed["final_k"] == 12
        assert completed["final_k_overridden"] is True

    def test_final_k_override_is_clamped_to_the_ceiling(self):
        # Algorithm-layer backstop (the resolver would 400 a value this large);
        # a value reaching the algorithm is still bounded to EVIDENCE_K_MAX.
        store, completed = self._run(
            make_algorithm(ScriptedLLM(), default_top_k=4),
            {"profile": "schnell", "final_k": EVIDENCE_K_MAX + 50},
        )
        assert store.search_top_ks == [EVIDENCE_K_MAX]
        assert completed["final_k"] == EVIDENCE_K_MAX

    def test_no_override_keeps_the_profile_factor(self):
        _, completed = self._run(
            make_algorithm(ScriptedLLM(), default_top_k=4),
            {"profile": "schnell"},
        )
        assert completed["final_k"] == 4  # schnell factor 1.0 * top_k 4
        assert completed["final_k_overridden"] is False

    def test_override_beats_the_profile_factor_in_the_decompose_branch(self):
        # `tief` decomposes, so the override must flow through the
        # interleave/merge `limit=final_k` path, not just the no-decompose one.
        # SUFFICIENT keeps the gate from looping; the override (20) beats the
        # factor result (4 * 2.0 = 8).
        _, completed = self._run(
            make_algorithm(ScriptedLLM([SUFFICIENT]), default_top_k=4),
            {"profile": "tief", "final_k": 20},
        )
        assert completed["final_k"] == 20
        assert completed["final_k_overridden"] is True


class TestCancellation:
    """Stop honors the run's cancel token at the pipeline checkpoints."""

    @staticmethod
    def _cancelled_context(context: RunContext, token: threading.Event) -> RunContext:
        return RunContext(
            providers=context.providers,
            strategies=None,
            agent_settings=context.agent_settings,
            cancel_token=token,
        )

    def test_pre_set_cancel_token_stops_before_any_provider_call(self):
        llm = ScriptedLLM([SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        token = threading.Event()
        token.set()
        with pytest.raises(AgentCancelled):
            run_with_profile(
                algorithm, runtime, self._cancelled_context(context, token)
            )
        assert llm.gate_prompts == []
        assert llm.answer_prompts == []
        assert store.search_top_ks == []

    def test_cancel_between_gate_rounds_stops_the_rewrite_loop(self):
        token = threading.Event()

        class CancelAfterFirstGate(ScriptedLLM):
            def complete_with_metadata(self, prompt, **kwargs):
                response = super().complete_with_metadata(prompt, **kwargs)
                if self.gate_prompts:
                    token.set()
                return response

        llm = CancelAfterFirstGate([REWRITE, SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm, grow=True)
        with pytest.raises(AgentCancelled):
            run_with_profile(
                algorithm,
                runtime,
                self._cancelled_context(context, token),
                profile="gruendlich",
            )
        assert len(llm.gate_prompts) == 1, "no second gate round after cancel"
        assert llm.answer_prompts == [], "synthesis never starts after cancel"

    def test_contextualization_fallback_does_not_swallow_cancellation(self):
        llm = ContextualizingLLM(AgentCancelled("stop"), [SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        token = threading.Event()
        with pytest.raises(AgentCancelled):
            run_followup(
                algorithm, runtime, self._cancelled_context(context, token)
            )
        assert store.search_top_ks == [], (
            "a cancel inside contextualization must never reach retrieval"
        )


    def test_pre_set_cancel_token_stops_before_the_contextualization_call(self):
        llm = ContextualizingLLM("Wie ist die Haftung geregelt?", [SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        token = threading.Event()
        token.set()
        with pytest.raises(AgentCancelled):
            run_followup(
                algorithm, runtime, self._cancelled_context(context, token)
            )
        assert llm.context_prompts == [], (
            "a pre-set token must not pay for the contextualization call"
        )
        assert store.search_top_ks == []

    def test_cancel_during_answer_regeneration_is_not_swallowed(self):
        """The graceful grounding fallback must never eat a cancellation."""

        class RegenerationCancelLLM(ScriptedLLM):
            def complete_with_metadata(self, prompt, **kwargs):
                if "You are a retrieval checker" in prompt:
                    return super().complete_with_metadata(prompt, **kwargs)
                self.answer_prompts.append(prompt)
                if len(self.answer_prompts) == 1:
                    return LLMResponse(
                        content=(
                            "QUOTES:\n"
                            '[K1] "Der Mond besteht aus Kaese."\n'
                            "\n"
                            "ANSWER:\n"
                            "Falsch belegt [K1]."
                        ),
                        prompt_tokens=5,
                        completion_tokens=5,
                        model="stub-answer",
                        finish_reason="stop",
                    )
                raise AgentCancelled("stop")

        llm = RegenerationCancelLLM()
        algorithm, store, context, runtime = make_algorithm(
            llm, gate_enabled=False, grounding_enabled=True
        )
        token = threading.Event()
        with pytest.raises(AgentCancelled):
            run_with_profile(
                algorithm, runtime, self._cancelled_context(context, token)
            )
        assert len(llm.answer_prompts) == 2, (
            "the unverified quote must trigger exactly one regeneration"
        )
