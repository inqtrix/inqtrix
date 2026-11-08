"""A run that a failure ends reports the state it reached, not its start.

The graph runner's input is not its evolving node state. A failure that
unwinds a node used to leave the runner with its input: the abort's progress
snapshot named no phase and round 0, which became the run's stored snapshot
(and with it the failed event's), and the provider usage recorded before the
failure was returned as none.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import inqtrix.nodes as nodes
from inqtrix.exceptions import AgentRateLimited
from inqtrix.graph import run
from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import AgentSettings
from inqtrix.strategies import create_default_strategies


class _Search:
    def search(self, *_args: Any, **_kwargs: Any) -> GroundedSearchResult:
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


class _LLM:
    models = SimpleNamespace(reasoning_model="stand-in")

    def is_available(self) -> bool:
        return True


def _nodes_failing_in_search(monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    def classify(s: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**s, "done": False, "round": 0}

    def plan(s: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {**s, "done": False, "queries": ["q1", "q2"]}

    def search(s: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        # Tokens spent by the searches that answered before the failure.
        s["total_prompt_tokens"] = int(s.get("total_prompt_tokens", 0) or 0) + 1_234
        raise failure

    for name, fn in {"classify": classify, "plan": plan, "search": search}.items():
        monkeypatch.setattr(nodes, name, fn)


def test_a_rate_limit_in_search_reports_the_phase_round_and_usage_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _nodes_failing_in_search(
        monkeypatch, AgentRateLimited("web-search-agent", RuntimeError("Error code: 429"))
    )
    events: list[tuple[str, dict[str, Any]]] = []
    settings = AgentSettings()

    result = run(
        "Welche Pflichten gelten ab 2027?",
        providers=ProviderContext(llm=_LLM(), search=_Search()),
        strategies=create_default_strategies(settings),
        settings=settings,
        run_event_sink=lambda kind, payload: events.append((kind, payload)),
    )

    assert result["usage"]["prompt_tokens"] == 1_234
    (aborted,) = [
        payload for kind, payload in events
        if kind == "inqtrix.progress.message" and "abgebrochen" in str(payload.get("message", "")).lower()
        or kind == "inqtrix.progress.message" and "aborted" in str(payload.get("message", "")).lower()
    ]
    assert aborted["snapshot"]["current_node"] == "search"
    assert aborted["snapshot"]["active_round"] == 1
    assert "429" not in aborted["message"]


def test_any_failure_in_search_reports_the_phase_and_round_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _nodes_failing_in_search(monkeypatch, RuntimeError("upstream broke"))
    events: list[tuple[str, dict[str, Any]]] = []
    settings = AgentSettings()

    with pytest.raises(RuntimeError, match="upstream broke"):
        run(
            "Welche Pflichten gelten ab 2027?",
            providers=ProviderContext(llm=_LLM(), search=_Search()),
            strategies=create_default_strategies(settings),
            settings=settings,
            run_event_sink=lambda kind, payload: events.append((kind, payload)),
        )

    # The last snapshot before the failure is what the run row stores and
    # what the failed event repeats.
    last = [payload["snapshot"] for _kind, payload in events if "snapshot" in payload][-1]
    assert last["current_node"] == "search"
    assert last["active_round"] == 1
