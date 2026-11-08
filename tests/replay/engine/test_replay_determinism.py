"""A whole research or knowledge run replays offline and sends the same model input.

The cassettes hold complete runs against the provider stand-in, answering from the
small invented corpus in ``stand_in_corpus/`` and with synthetic search results, so
no real hit and no recorded answer reaches this repository. Recording is an explicit
maintainer step and costs nothing, because nothing leaves the machine:

    INQTRIX_MOCK_CORPUS=tests/replay/engine/stand_in_corpus \\
      INQTRIX_MOCK_LATENCY_SCALE=0 INQTRIX_MOCK_SEARCH_RESULTS=2 INQTRIX_MOCK_EMBEDDING_DIM=8 \\
      uv run uvicorn mock_providers:app --app-dir tests/load/mock-providers --port 9300
    INQTRIX_RECORD_MODE=once uv run pytest tests/replay/engine/test_replay_determinism.py

Delete a cassette first to record it anew.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

import pytest
import vcr
from vcr.cassette import Cassette

from inqtrix.agent import ResearchAgent
from inqtrix.core.context import RunContext, RuntimeContext
from inqtrix.core.results import RunRequest
from inqtrix.graph import run_test
from inqtrix.knowledge import algorithm as knowledge_algorithm
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.providers.azure import AzureOpenAILLM
from inqtrix.providers.base import (
    ProviderContext,
    with_untrusted_content_rule_provider,
)
from inqtrix.providers.perplexity import PerplexitySearch
from inqtrix.services.knowledge_service import KnowledgeService
from inqtrix.settings import AgentSettings, Settings
from inqtrix.strategies import create_default_strategies
from tests.contract._app import StubSearch
from tests.replay.engine.body_matcher import same_normalized_body
from tests.test_knowledge_engine import StubEmbeddings

pytestmark = pytest.mark.replay

_STAND_IN = "http://127.0.0.1:9300"
_TODAY = "15. March 2031"
_RESEARCH_QUESTION = "Wie arbeitet das Modellgerät, und welche Kennzahl gilt als gut?"
_KNOWLEDGE_QUESTION = "Wie arbeitet das Modellgerät?"
_DOCUMENT = (
    "Das Modellgerät arbeitet in zwei Stufen. Die Kennzahl des Modellgeräts wird "
    "über ein Jahr gemessen. Eine Kennzahl über drei gilt als gut."
)
_CASSETTES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "cassettes" / "engine"


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prompts carry the date, and a cassette must not expire with the day."""
    monkeypatch.setattr("inqtrix.nodes.today", lambda: _TODAY)


@contextmanager
def _replay(name: str, vcr_config: dict) -> Iterator[Any]:
    recorder = vcr.VCR(cassette_library_dir=str(_CASSETTES), **vcr_config)
    recorder.register_matcher("normalized_body", same_normalized_body)
    with recorder.use_cassette(f"{name}.yaml") as cassette:
        yield cassette


@contextmanager
def _complete_replay(name: str, vcr_config: dict) -> Iterator[None]:
    with _replay(name, vcr_config) as cassette:
        yield
    # A model call the engine stopped making is a change of its input as well.
    if vcr_config["record_mode"] == "none":
        assert cassette.all_played, f"{name}: {len(cassette) - cassette.play_count} calls unused"


def _providers(search: Any) -> ProviderContext:
    """With the rule production adds to what every model call receives."""
    llm = AzureOpenAILLM(
        azure_endpoint=f"{_STAND_IN}/azure",
        api_key="stand-in",
        default_model="gpt-5.4",
    )
    return ProviderContext(
        llm=with_untrusted_content_rule_provider(llm), search=search
    )


def _research_export() -> dict[str, Any]:
    """One research run; everything but the wall clock and the logs of it."""
    settings = AgentSettings(testing_mode=True)
    providers = _providers(
        PerplexitySearch("stand-in", base_url=f"{_STAND_IN}/perplexity")
    )
    result = run_test(
        _RESEARCH_QUESTION,
        providers=providers,
        strategies=create_default_strategies(settings, llm=providers.llm),
        settings=settings,
    )
    return {
        key: value
        for key, value in result.items()
        if key not in {"metrics", "iteration_logs"}
    }


def _knowledge_export() -> dict[str, Any]:
    """One knowledge run over a one-document collection in the memory store."""
    knowledge = KnowledgeProviderContext(
        embeddings=StubEmbeddings(), store=MemoryKnowledgeStore(), default_top_k=2
    )
    service = KnowledgeService(
        knowledge=knowledge, chunk_max_chars=2_000, max_document_chars=100_000
    )

    async def seed() -> str:
        collection = await service.create_collection(name="Modellgerät")
        await service.add_document(
            collection_id=collection.id, title="Handbuch", text=_DOCUMENT
        )
        return collection.id

    collection_id = asyncio.run(seed())
    settings = Settings()
    runtime = RuntimeContext(
        settings=settings,
        registry=None,
        providers=_providers(StubSearch()),
        strategies=None,
    )
    result = KnowledgeAlgorithm(knowledge=knowledge, gate_enabled=True).run(
        RunRequest(
            mode="knowledge",
            question=_KNOWLEDGE_QUESTION,
            knowledge_filters={"collection_ids": [collection_id]},
        ),
        runtime=runtime,
        context=RunContext(
            providers=runtime.providers,
            strategies=None,
            agent_settings=settings.agent,
        ),
    )
    return result.model_dump(mode="json")


_WALL_CLOCK = re.compile(r"(_seconds|_ms|_at|elapsed)$")
_DRAWN_IDENTIFIER = re.compile(r"\b([a-z]{2,5})_[0-9a-f]{12,}\b")


def _masked(text: str) -> str:
    return _DRAWN_IDENTIFIER.sub(r"\1_<id>", text)


def _bytes(value: Any) -> str:
    """The export without what differs by design: wall clock and drawn identifiers.

    Keys are masked before they are sorted, because a key that is a drawn identifier
    would otherwise order the entries differently each time.
    """
    if isinstance(value, dict):
        entries = sorted(
            f"{json.dumps(_masked(key))}:{_bytes(item)}"
            for key, item in value.items()
            if not _WALL_CLOCK.search(key)
        )
        return "{" + ",".join(entries) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_bytes(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(_masked(value), ensure_ascii=False)
    return json.dumps(value)


_ENGINES: dict[str, Callable[[], dict[str, Any]]] = {
    "research": _research_export,
    "knowledge": _knowledge_export,
}


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_two_replays_give_the_same_export(engine: str, vcr_config: dict) -> None:
    with _complete_replay(engine, vcr_config):
        first = _ENGINES[engine]()
    with _complete_replay(engine, vcr_config):
        second = _ENGINES[engine]()

    assert _bytes(first) == _bytes(second)
    assert first["answer"]


def _library_agent(monkeypatch: pytest.MonkeyPatch) -> ResearchAgent:
    """An agent on the engine run's stand-in, with providers and their cache of its own."""
    settings = AgentSettings(testing_mode=True)
    providers = _providers(
        PerplexitySearch("stand-in", base_url=f"{_STAND_IN}/perplexity")
    )
    strategies = create_default_strategies(settings, llm=providers.llm)
    agent = ResearchAgent()
    monkeypatch.setattr(
        agent, "_ensure_initialised", lambda: (providers, strategies, settings)
    )
    return agent


def test_the_library_facade_replays_the_research_run(
    vcr_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``research()`` and ``aresearch()`` replay the engine's run, call for call.

    Each replays the complete cassette, so the facade hands the graph exactly
    what the engine run gets, and both give the engine's answer.
    """
    with _complete_replay("research", vcr_config):
        engine_answer = _research_export()["answer"]
    with _complete_replay("research", vcr_config):
        blocking = _library_agent(monkeypatch).research(_RESEARCH_QUESTION)
    with _complete_replay("research", vcr_config):
        awaited = asyncio.run(_library_agent(monkeypatch).aresearch(_RESEARCH_QUESTION))

    assert blocking.answer == engine_answer
    assert _bytes(blocking.model_dump(mode="json")) == _bytes(awaited.model_dump(mode="json"))


class _Miss(BaseException):
    """Ends a run at its first unknown request; the engine handles only ``Exception``."""


def _change_model_input(engine: str, monkeypatch: pytest.MonkeyPatch) -> None:
    if engine == "research":
        monkeypatch.setattr("inqtrix.nodes.today", lambda: "16. March 2031")
        return
    real = knowledge_algorithm.build_knowledge_answer_prompt

    def changed(*args: Any, **kwargs: Any) -> str:
        return f"{real(*args, **kwargs)}\nGeänderte Vorgabe."

    monkeypatch.setattr(knowledge_algorithm, "build_knowledge_answer_prompt", changed)


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_a_changed_model_input_does_not_replay(
    engine: str, vcr_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_can_play = Cassette.can_play_response_for

    def strict(self: Cassette, request: Any) -> bool:
        if not real_can_play(self, request):
            raise _Miss(request.path)
        return True

    monkeypatch.setattr(Cassette, "can_play_response_for", strict)
    _change_model_input(engine, monkeypatch)

    with pytest.raises(_Miss), _replay(engine, vcr_config):
        _ENGINES[engine]()


def test_a_model_call_the_engine_stopped_making_counts_as_a_change(
    vcr_config: dict,
) -> None:
    with pytest.raises(AssertionError, match="calls unused"):
        with _complete_replay("knowledge", vcr_config):
            pass


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_a_cassette_holds_no_real_source(engine: str) -> None:
    """Only the stand-in and its invented sources may appear: this repository is public."""
    text = (_CASSETTES / f"{engine}.yaml").read_text(encoding="utf-8")
    hosts = {urlsplit(url).hostname for url in re.findall(r"https?://[^\s\"'\\)\]]+", text)}

    assert hosts <= {"127.0.0.1", "example.test"}, hosts
