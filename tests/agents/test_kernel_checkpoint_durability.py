"""Kernel checkpoints are durable before the next step starts.

The kernel runs its graph with LangGraph's ``durability="sync"``: the
checkpoint of a superstep is stored before the next superstep begins.
A tool step therefore always finds the model step that requested it in
the stored state, and the loop cannot run ahead of LangGraph's background
checkpoint chain. Under the default ``"async"`` mode a run with many fast
supersteps can starve that chain's own thread pool and never finish.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

pytest.importorskip("deepagents")

from langgraph.checkpoint.memory import InMemorySaver

from inqtrix.search_result import GroundedSearchResult, GroundedSource

from tests.agents.test_kernel_prompt_events import (
    ScriptedToolLLM,
    _text_turn,
    _web_turn,
    make_client,
    wait_status,
)

# Upper bound of the held checkpoint write. With the correct order the tool
# cannot start before the write, so every passing run waits this long once.
HOLD_SECONDS = 3.0


class HeldCheckpointSaver(InMemorySaver):
    """Holds the first checkpoint stored after the model requested a tool.

    The hold ends when the tool body releases it, or after
    ``HOLD_SECONDS``, so neither the correct order nor its regression can
    hang the run.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._armed = False
        self._held = False
        self.release = threading.Event()
        self.held_checkpoint_stored = threading.Event()

    def put_writes(self, config, writes, task_id, task_path=""):
        for channel, value in writes:
            messages = value if isinstance(value, list) else [value]
            if channel == "messages" and any(
                getattr(message, "tool_calls", None) for message in messages
            ):
                with self._lock:
                    if not self._held:
                        self._armed = True
        return super().put_writes(config, writes, task_id, task_path)

    def put(self, config, checkpoint, metadata, new_versions):
        with self._lock:
            hold = self._armed and not self._held
            if hold:
                self._armed = False
                self._held = True
        if hold:
            self.release.wait(timeout=HOLD_SECONDS)
        stored = super().put(config, checkpoint, metadata, new_versions)
        if hold:
            self.held_checkpoint_stored.set()
        return stored


class CheckpointProbingSearch:
    """Web search whose body records whether the held checkpoint is stored."""

    def __init__(self, saver: HeldCheckpointSaver) -> None:
        self._saver = saver
        self.saw_model_step_stored: bool | None = None

    def is_available(self) -> bool:
        return True

    def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
        if self.saw_model_step_stored is None:
            self.saw_model_step_stored = self._saver.held_checkpoint_stored.is_set()
        self._saver.release.set()
        return GroundedSearchResult(
            answer=f"Web zu {query}",
            sources=[
                GroundedSource(url="https://example.com", title="Q", snippet="s")
            ],
        )


def submit(client) -> str:
    response = client.post(
        "/v1/runs",
        json={
            "question": "Was gilt hier?",
            "mode": "agent_kernel",
            "autonomy": "autonomous",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_tool_step_starts_after_the_model_step_checkpoint_is_stored(monkeypatch):
    saver = HeldCheckpointSaver()
    # The kernel builds its volatile saver on first use from this name.
    monkeypatch.setattr(
        "langgraph.checkpoint.memory.InMemorySaver", lambda: saver
    )
    search = CheckpointProbingSearch(saver)
    llm = ScriptedToolLLM([_web_turn(), _text_turn("Fertig.")])
    client = make_client(llm, search=search)
    with client:
        summary = wait_status(
            client,
            submit(client),
            {"completed", "failed", "waiting_for_input"},
            timeout=HOLD_SECONDS + 10.0,
        )
    assert summary["status"] == "completed", summary
    # Anchor: the web tool really ran inside this run.
    assert search.saw_model_step_stored is not None
    # The checkpoint holding the model's tool call was stored before the
    # tool body started.
    assert search.saw_model_step_stored is True


def test_kernel_streams_its_graph_with_sync_durability(monkeypatch):
    from inqtrix.agents.kernel.algorithm import KernelAgentAlgorithm

    seen: list[Any] = []
    compile_graph = KernelAgentAlgorithm._compiled_graph

    def spying_compiled_graph(self, *args, **kwargs):
        graph = compile_graph(self, *args, **kwargs)

        class StreamSpy:
            def __getattr__(self, name: str) -> Any:
                return getattr(graph, name)

            def stream(self, *stream_args, **stream_kwargs):
                seen.append(stream_kwargs.get("durability"))
                return graph.stream(*stream_args, **stream_kwargs)

        return StreamSpy()

    monkeypatch.setattr(
        KernelAgentAlgorithm, "_compiled_graph", spying_compiled_graph
    )
    llm = ScriptedToolLLM([_text_turn("Fertig.")])
    client = make_client(llm)
    with client:
        summary = wait_status(
            client, submit(client), {"completed", "failed", "waiting_for_input"}
        )
    assert summary["status"] == "completed", summary
    assert seen == ["sync"]
