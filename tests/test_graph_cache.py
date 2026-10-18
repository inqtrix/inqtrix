"""Regression tests for the compiled LangGraph cache."""

from __future__ import annotations

import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

import inqtrix.graph as graph_module
from inqtrix.settings import AgentSettings


@pytest.fixture(autouse=True)
def _clear_graph_cache() -> Iterator[None]:
    with graph_module._graph_cache_lock:
        graph_module._graph_cache.clear()
    yield
    with graph_module._graph_cache_lock:
        graph_module._graph_cache.clear()


def _install_fake_graph_builder(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay_s: float = 0.0,
) -> list[int]:
    builds: list[int] = []

    def fake_default_graph_config(
        providers: object,
        strategies: object,
        settings: AgentSettings,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            providers=providers,
            strategies=strategies,
            settings=settings,
        )

    def fake_build_graph(config: Any) -> SimpleNamespace:
        if delay_s:
            time.sleep(delay_s)
        builds.append(int(config.settings.max_rounds))
        return SimpleNamespace(
            max_rounds=config.settings.max_rounds,
            settings=config.settings,
        )

    monkeypatch.setattr(graph_module, "default_graph_config", fake_default_graph_config)
    monkeypatch.setattr(graph_module, "build_graph", fake_build_graph)
    return builds


def test_get_agent_reuses_graph_for_same_effective_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = _install_fake_graph_builder(monkeypatch)
    providers = object()
    strategies = object()

    agent_a = graph_module.get_agent(providers, strategies, AgentSettings())
    agent_b = graph_module.get_agent(
        providers,
        strategies,
        AgentSettings(max_rounds=AgentSettings().max_rounds),
    )

    assert agent_a is agent_b
    assert builds == [AgentSettings().max_rounds]


def test_get_agent_keeps_distinct_graphs_for_distinct_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builds = _install_fake_graph_builder(monkeypatch)
    providers = object()
    strategies = object()

    agent_a = graph_module.get_agent(providers, strategies, AgentSettings(max_rounds=2))
    agent_b = graph_module.get_agent(providers, strategies, AgentSettings(max_rounds=3))

    assert agent_a is not agent_b
    assert agent_a.max_rounds == 2
    assert agent_b.max_rounds == 3
    assert builds == [2, 3]


def test_get_agent_parallel_calls_return_matching_settings_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_graph_builder(monkeypatch, delay_s=0.001)
    providers = object()
    strategies = object()
    settings_by_round = {
        2: AgentSettings(max_rounds=2),
        3: AgentSettings(max_rounds=3),
    }
    requested = [2, 3] * 100

    def call_get_agent(max_rounds: int) -> int:
        agent = graph_module.get_agent(
            providers,
            strategies,
            settings_by_round[max_rounds],
        )
        return int(agent.max_rounds)

    with ThreadPoolExecutor(max_workers=16) as pool:
        observed = list(pool.map(call_get_agent, requested))

    assert observed == requested


def test_get_agent_cache_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_graph_builder(monkeypatch)
    providers = object()
    strategies = object()

    for index in range(graph_module._GRAPH_CACHE_MAXSIZE + 8):
        graph_module.get_agent(
            providers,
            strategies,
            AgentSettings(max_question_length=10_000 + index),
        )

    first_key = (
        id(providers),
        id(strategies),
        graph_module._settings_fingerprint(AgentSettings(max_question_length=10_000)),
    )
    last_key = (
        id(providers),
        id(strategies),
        graph_module._settings_fingerprint(
            AgentSettings(
                max_question_length=10_000 + graph_module._GRAPH_CACHE_MAXSIZE + 7,
            ),
        ),
    )
    with graph_module._graph_cache_lock:
        assert len(graph_module._graph_cache) == graph_module._GRAPH_CACHE_MAXSIZE
        assert first_key not in graph_module._graph_cache
        assert last_key in graph_module._graph_cache
