"""Decomposition-stage tests: parse matrix and the deep-profile path.

The decomposition call follows the gate's failure contract: clean
parses (including the deliberate ``[]`` "single-aspect" answer) carry
the parsed marker, anything unparseable degrades to a no-op with the
loud fallback marker — never a crashed run, never silent.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from inqtrix.knowledge.decompose import (
    DECOMPOSE_MARKER_FALLBACK,
    DECOMPOSE_MARKER_PARSED,
    decompose_question,
)
from inqtrix.knowledge.retrieval import interleave_candidates
from inqtrix.knowledge.stores.ports import DocumentChunk, RetrievalCandidate
from inqtrix.providers.base import LLMResponse

from tests.test_knowledge_algorithm_profiles import (
    SUFFICIENT,
    ScriptedLLM,
    make_algorithm,
    run_with_profile,
)


class OneShotLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        return LLMResponse(
            content=self._content,
            prompt_tokens=7,
            completion_tokens=4,
            model="stub",
            finish_reason="stop",
        )


def decompose(content: str):
    decomposition, usage = decompose_question(
        OneShotLLM(content),
        question="Welche Pflichten gelten fuer Backups und Verschluesselung?",
        model=None,
        timeout=30.0,
    )
    assert usage == {"prompt_tokens": 7, "completion_tokens": 4}
    return decomposition


class TestParsing:
    def test_clean_split_parses(self):
        decomposition = decompose(
            json.dumps(
                [
                    "Welche Pflichten gelten fuer Backups?",
                    "Welche Pflichten gelten fuer Verschluesselung?",
                ]
            )
        )
        assert decomposition.marker == DECOMPOSE_MARKER_PARSED
        assert len(decomposition.sub_queries) == 2

    def test_explicit_empty_array_is_a_parsed_no_split(self):
        decomposition = decompose("[]")
        assert decomposition.marker == DECOMPOSE_MARKER_PARSED
        assert decomposition.sub_queries == ()

    def test_single_entry_collapses_to_no_split(self):
        """One sub-query is no decomposition — retrieving a clone of
        the question would double cost for nothing."""
        decomposition = decompose('["Welche Pflichten gelten?"]')
        assert decomposition.marker == DECOMPOSE_MARKER_PARSED
        assert decomposition.sub_queries == ()

    def test_overlong_lists_are_capped(self):
        decomposition = decompose(
            json.dumps([f"Teilfrage {index}?" for index in range(8)])
        )
        assert len(decomposition.sub_queries) == 4

    @pytest.mark.parametrize(
        "content",
        ["kein json", '{"sub": "queries"}', "[1, 2, 3]", '["a", 2]', ""],
    )
    def test_unparseable_degrades_loudly(self, content):
        decomposition = decompose(content)
        assert decomposition.marker == DECOMPOSE_MARKER_FALLBACK
        assert decomposition.sub_queries == ()

    def test_prose_wrapped_array_still_parses(self):
        decomposition = decompose(
            'Hier die Zerlegung: ["Frage A?", "Frage B?"] — viel Erfolg!'
        )
        assert decomposition.marker == DECOMPOSE_MARKER_PARSED
        assert len(decomposition.sub_queries) == 2


def make_candidate(chunk_id: str) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk=DocumentChunk(
            id=chunk_id,
            document_id=f"doc-{chunk_id}",
            collection_id="c1",
            chunk_index=0,
            text=f"Text {chunk_id}",
        ),
        score=1.0,
        document_title=f"Dokument {chunk_id}",
    )


class TestInterleave:
    def test_every_aspect_contributes_to_the_top_k(self):
        """Round-robin: with limit 4 and three lists, no single list
        may occupy all slots."""
        lists = [
            [make_candidate(f"a{i}") for i in range(4)],
            [make_candidate(f"b{i}") for i in range(4)],
            [make_candidate(f"c{i}") for i in range(4)],
        ]
        merged = interleave_candidates(lists, limit=4)
        ids = [candidate.chunk.id for candidate in merged]
        assert ids == ["a0", "b0", "c0", "a1"]

    def test_duplicates_collapse_on_chunk_id(self):
        shared = make_candidate("shared")
        lists = [
            [shared, make_candidate("a1")],
            [make_candidate("shared"), make_candidate("b1")],
        ]
        merged = interleave_candidates(lists, limit=4)
        ids = [candidate.chunk.id for candidate in merged]
        assert ids == ["shared", "b1", "a1"]

    def test_exhausted_lists_do_not_stall_the_rotation(self):
        lists = [
            [make_candidate("a0")],
            [make_candidate(f"b{i}") for i in range(3)],
        ]
        merged = interleave_candidates(lists, limit=10)
        ids = [candidate.chunk.id for candidate in merged]
        assert ids == ["a0", "b0", "b1", "b2"]


class DecomposingLLM(ScriptedLLM):
    """Adds a decomposition response in front of the scripted flows."""

    def __init__(self, decomposition: list[str], gate_verdicts) -> None:
        super().__init__(gate_verdicts)
        self._decomposition = decomposition
        self.decompose_prompts: list[str] = []

    def complete_with_metadata(self, prompt: str, **kwargs: Any) -> LLMResponse:
        if "JSON array of sub-questions" in prompt:
            self.decompose_prompts.append(prompt)
            return LLMResponse(
                content=json.dumps(self._decomposition),
                prompt_tokens=6,
                completion_tokens=2,
                model="stub-decompose",
                finish_reason="stop",
            )
        return super().complete_with_metadata(prompt, **kwargs)


class TestDeepProfilePath:
    def test_tief_decomposes_and_retrieves_per_sub_query(self):
        llm = DecomposingLLM(
            ["Wie ist die Haftung begrenzt?", "Was regelt die Verguetung?"],
            [SUFFICIENT],
        )
        algorithm, store, context, runtime = make_algorithm(llm)
        events: list = []
        result = run_with_profile(
            algorithm, runtime, context, "tief", events=events
        )
        assert len(llm.decompose_prompts) == 1
        # Original question + two sub-queries = three retrievals.
        assert len(store.search_top_ks) == 3
        state = result.raw["result_state"]
        assert len(state["queries"]) == 3
        payloads = dict(events)
        decomposition = payloads["inqtrix.knowledge.decomposition.completed"]
        assert decomposition["sub_query_count"] == 2
        assert decomposition["marker"] == DECOMPOSE_MARKER_PARSED
        # The deep profile answers as a structured report.
        assert "## Kurzfazit" in llm.answer_prompts[0]

    def test_tief_single_aspect_runs_one_retrieval(self):
        llm = DecomposingLLM([], [SUFFICIENT])
        algorithm, store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "tief")
        assert len(store.search_top_ks) == 1

    def test_standard_never_calls_decomposition(self):
        llm = DecomposingLLM(["sollte", "nie", "passieren"], [SUFFICIENT])
        algorithm, _store, context, runtime = make_algorithm(llm)
        run_with_profile(algorithm, runtime, context, "standard")
        assert llm.decompose_prompts == []
        assert "## Kurzfazit" not in llm.answer_prompts[0]
