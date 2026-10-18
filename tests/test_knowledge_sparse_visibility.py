"""Cross-lingual visibility: the BM25 tokenizer-mismatch marker.

These pin the honest, narrowly-scoped behaviour (no per-collection
language):

* a CONFIDENT query-vs-tokenizer language mismatch surfaces a marker + event +
  redacted log (No Silent Fallbacks),
* the same-language default path stays field-identical (no new field/event/log),
* a store without a lexical branch never flags,
* the stores expose the read-only ``sparse_language`` property the algorithm and
  the capability manifest read via ``getattr``.

The deeper "German query vs English documents" case is deliberately NOT covered
here — it needs per-collection language, which collections do not record.
"""

from __future__ import annotations

import logging

from inqtrix.core.context import RunContext
from inqtrix.core.results import RunRequest
from inqtrix.knowledge.algorithm import SPARSE_MARKER_TOKENIZER_MISMATCH

from tests.test_knowledge_algorithm_profiles import ScriptedLLM, make_algorithm


def _run(
    algorithm,
    runtime,
    context,
    question: str,
    *,
    events: list | None = None,
):
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
            question=question,
            # `schnell` = one answer call, no gate scripting needed.
            knowledge_filters={"profile": "schnell"},
        ),
        runtime=runtime,
        context=context,
    )


def _attach_inqtrix_caplog(caplog):
    # gotcha #2: the inqtrix logger does not always propagate to root, so a
    # caplog assertion needs the handler attached explicitly.
    caplog.set_level(logging.WARNING, logger="inqtrix")
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    return logger


class TestTokenizerMismatchMarker:
    def test_english_query_against_german_bm25_flags_mismatch(self, caplog):
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        # Simulate an active BM25 lexical branch (german tokenizer).
        store.sparse_language = "de"
        events: list[tuple[str, dict]] = []
        logger = _attach_inqtrix_caplog(caplog)
        try:
            result = _run(
                algorithm,
                runtime,
                context,
                "How is the liability defined in the contract?",
                events=events,
            )
        finally:
            logger.removeHandler(caplog.handler)

        sparse = result.raw["result_state"]["knowledge_sparse"]
        assert sparse["marker"] == SPARSE_MARKER_TOKENIZER_MISMATCH
        assert sparse["query_language"] == "en"
        assert sparse["sparse_language"] == "de"

        emitted = [event for event, _ in events]
        assert "inqtrix.knowledge.sparse.tokenizer_mismatch" in emitted
        payload = dict(events)["inqtrix.knowledge.sparse.tokenizer_mismatch"]
        assert payload["query_language"] == "en"
        assert payload["sparse_language"] == "de"

        # Visible (marker + language codes) but the query text is never logged
        # (sensitivity discipline: no content word from the query may leak).
        assert SPARSE_MARKER_TOKENIZER_MISMATCH in caplog.text
        for word in ("liability", "defined", "contract"):
            assert word not in caplog.text

    def test_same_language_adds_no_field_or_event(self, caplog):
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        store.sparse_language = "de"
        events: list[tuple[str, dict]] = []
        logger = _attach_inqtrix_caplog(caplog)
        try:
            result = _run(
                algorithm,
                runtime,
                context,
                "Wie ist die Haftung im Vertrag geregelt?",
                events=events,
            )
        finally:
            logger.removeHandler(caplog.handler)

        # Byte-/field-identity on the default same-language path.
        assert "knowledge_sparse" not in result.raw["result_state"]
        assert all(
            event != "inqtrix.knowledge.sparse.tokenizer_mismatch"
            for event, _ in events
        )
        assert SPARSE_MARKER_TOKENIZER_MISMATCH not in caplog.text

    def test_ambiguous_query_does_not_flag(self):
        # The confident detector returns None on a term-only query, so even
        # against a german tokenizer there is no false alarm.
        llm = ScriptedLLM()
        algorithm, store, context, runtime = make_algorithm(llm)
        store.sparse_language = "de"
        result = _run(algorithm, runtime, context, "DSGVO Pflichten Auftragswert")
        assert "knowledge_sparse" not in result.raw["result_state"]

    def test_store_without_lexical_branch_never_flags(self):
        # The default memory store carries no `sparse_language` -> getattr None,
        # so an English query produces no marker (dense-only store).
        llm = ScriptedLLM()
        algorithm, _store, context, runtime = make_algorithm(llm)
        result = _run(
            algorithm, runtime, context, "How is the liability defined?"
        )
        assert "knowledge_sparse" not in result.raw["result_state"]

    def test_marker_is_the_only_result_state_delta(self):
        # Field-identity (stronger than "field absent"): the mismatch path adds
        # EXACTLY `knowledge_sparse` and nothing else; the same-language path
        # adds nothing. Guards against a future change that silently reshapes
        # result_state on the cross-lingual path.
        algorithm, store, context, runtime = make_algorithm(ScriptedLLM())
        store.sparse_language = "de"
        en_state = _run(
            algorithm, runtime, context, "How is the liability defined?"
        ).raw["result_state"]

        algorithm2, store2, context2, runtime2 = make_algorithm(ScriptedLLM())
        store2.sparse_language = "de"
        de_state = _run(
            algorithm2, runtime2, context2, "Wie ist die Haftung geregelt?"
        ).raw["result_state"]

        assert set(en_state) - set(de_state) == {"knowledge_sparse"}
        assert set(de_state) - set(en_state) == set()


class TestSparseLanguageProperty:
    def test_qdrant_vector_index_reports_de_or_none(self):
        from inqtrix.knowledge.stores.qdrant_store import (
            QdrantKnowledgeStore,
            QdrantVectorIndex,
        )

        active = QdrantVectorIndex(
            url="http://localhost:6333", sparse="bm25_german"
        )
        assert active.sparse_language == "de"
        off = QdrantVectorIndex(url="http://localhost:6333", sparse="off")
        assert off.sparse_language is None

        legacy = QdrantKnowledgeStore(
            url="http://localhost:6333", sparse="bm25_german"
        )
        assert legacy.sparse_language == "de"

    def test_postgres_store_delegates_to_real_vector_index(self):
        # Real construction (no DB connection: create_async_engine is lazy), so
        # this exercises the actual wiring — __init__ stores vector_index as
        # self._vectors AND the property delegates to it. Goes red if __init__
        # stops wiring the index or the delegation breaks. The canonical
        # full-stack path Postgres -> QdrantVectorIndex reports "de"; a vector
        # index without the property (MemoryVectorIndex) reports None.
        from inqtrix.knowledge.stores.postgres_store import PostgresKnowledgeStore
        from inqtrix.knowledge.stores.qdrant_store import QdrantVectorIndex
        from inqtrix.knowledge.stores.vector_index import MemoryVectorIndex
        from inqtrix.storage.db import build_engine

        engine = build_engine(
            "postgresql+asyncpg://user:pw@localhost/db", null_pool=True
        )
        hybrid = PostgresKnowledgeStore(
            engine=engine,
            app_role="app",
            vector_index=QdrantVectorIndex(
                url="http://localhost:6333", sparse="bm25_german"
            ),
        )
        assert hybrid.sparse_language == "de"

        dense_only = PostgresKnowledgeStore(
            engine=engine, app_role="app", vector_index=MemoryVectorIndex()
        )
        assert dense_only.sparse_language is None
