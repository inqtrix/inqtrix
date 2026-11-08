"""Every engine binds its run's cancel probe once, at its entry.

A cancelled run must end the provider call it waits for, wherever in the run
that call is made. The probe used to be bound at single call sites: the
chat-only path, the review passes and tools of the kernel and the whole
workspace agent never bound it, so a stop there waited for the call to finish.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.agents.algorithm import WorkspaceAgentAlgorithm
from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm
from inqtrix.graph import run
from inqtrix.knowledge.algorithm import KnowledgeAlgorithm
from inqtrix.providers import base as provider_base
from inqtrix.providers.base import LLMResponse, ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import AgentSettings
from inqtrix.strategies import create_default_strategies


class _Search:
    def search(self, *_args: Any, **_kwargs: Any) -> GroundedSearchResult:
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


class _ChatLLM:
    """Answers the chat-only call and keeps the probe bound while it ran."""

    def __init__(self) -> None:
        self.models = SimpleNamespace(reasoning_model="direct-model")
        self.bound: list[Any] = []

    def complete_with_metadata(self, prompt: str, **_kwargs: Any) -> LLMResponse:
        self.bound.append(provider_base._cancel_probe.get())
        return LLMResponse(content="Antwort", prompt_tokens=1, completion_tokens=1, model="direct-model")

    def is_available(self) -> bool:
        return True


def test_the_chat_only_call_ends_with_its_run() -> None:
    cancel = threading.Event()
    settings = AgentSettings(skip_search=True)
    llm = _ChatLLM()

    run(
        "Hallo",
        providers=ProviderContext(llm=llm, search=_Search()),
        strategies=create_default_strategies(settings),
        settings=settings,
        cancel_event=cancel,
    )

    (bound,) = llm.bound
    assert bound is not None and bound.probe == cancel.is_set
    assert provider_base._cancel_probe.get() is None, "the binding must not outlive the run"


def test_a_research_node_sees_the_probe_its_run_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    import inqtrix.graph as graph_module

    seen: list[Any] = []

    class _Graph:
        def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
            seen.append(provider_base._cancel_probe.get())
            return {**state, "answer": "fertig"}

    monkeypatch.setattr(graph_module, "get_agent", lambda *_args, **_kwargs: _Graph())
    cancel = threading.Event()
    settings = AgentSettings()

    run(
        "Hallo",
        providers=ProviderContext(llm=_ChatLLM(), search=_Search()),
        strategies=create_default_strategies(settings),
        settings=settings,
        cancel_event=cancel,
    )

    (bound,) = seen
    assert bound is not None and bound.probe == cancel.is_set


@pytest.mark.parametrize(
    ("engine", "inner"),
    [
        (KernelAgentAlgorithm, "_run_in_cancel_scope"),
        (WorkspaceAgentAlgorithm, "_run_in_cancel_scope"),
        (KnowledgeAlgorithm, "_run_pipeline"),
    ],
    ids=["agent_kernel", "workspace_agent", "knowledge"],
)
def test_an_engine_binds_the_probe_for_its_whole_run(
    monkeypatch: pytest.MonkeyPatch, engine: type, inner: str
) -> None:
    seen: list[Any] = []

    def body(self: Any, request: Any, *, runtime: Any, context: Any) -> str:
        seen.append(provider_base._cancel_probe.get())
        return "result"

    monkeypatch.setattr(engine, inner, body)
    cancel = threading.Event()

    result = object.__new__(engine).run(
        object(), runtime=object(), context=SimpleNamespace(cancel_token=cancel)
    )

    assert result == "result"
    (bound,) = seen
    assert bound is not None and bound.probe == cancel.is_set
    assert provider_base._cancel_probe.get() is None
