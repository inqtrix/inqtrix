"""Shared app builder and fakes for the contract test suite.

Builds the app through the public :func:`inqtrix.server.app.create_app`
factory (not ``register_routes`` directly) so the tests exercise exactly
the surface a deployment sees. Providers are network-free stubs; the
agent entry point is monkeypatched per test at the ONE graph seam
``inqtrix.research.web_research.run_web_graph`` — streamed AND non-streamed
chat, native runs, and test runs all dispatch through the registry and reach
the engine there (streaming has no separate binding).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from inqtrix.providers.base import ProviderContext
from inqtrix.search_result import GroundedSearchResult
from inqtrix.server.app import create_app
from inqtrix.settings import (
    AgentSettings,
    ModelSettings,
    ServerSettings,
    Settings,
    StorageSettings,
)


class StubLLM:
    """Network-free LLM provider stub for contract tests."""

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return "ok"

    def is_available(self) -> bool:
        return True


class StubSearch:
    """Network-free search provider stub for contract tests."""

    def search(self, *args: Any, **kwargs: Any) -> GroundedSearchResult:
        return GroundedSearchResult()

    def is_available(self) -> bool:
        return True


def make_contract_client(
    *,
    agent_settings: AgentSettings | None = None,
    server_settings: ServerSettings | None = None,
) -> TestClient:
    """Build a TestClient over the public ``create_app`` factory."""
    settings = Settings(
        models=ModelSettings(),
        agent=agent_settings or AgentSettings(),
        server=server_settings or ServerSettings(),
        # Pinned so a developer .env configuring the Postgres backend
        # can never leak external IO into the offline contract suite.
        storage=StorageSettings(backend="memory", database_url=""),
    )
    providers = ProviderContext(llm=StubLLM(), search=StubSearch())
    app = create_app(settings=settings, providers=providers)
    return TestClient(app)


def minimal_agent_result(
    answer: str = "Antwort mit Quelle [1].",
    *,
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
    model_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the smallest ``graph.run``-shaped result the routes accept.

    Mirrors the contract between ``graph.run`` and the HTTP layer: the
    routes read ``answer``, ``usage`` and ``result_state`` (which
    ``ResearchResult.from_raw`` consumes for the native run result
    payload). ``model_resolution`` lands under
    ``result_state.node_model_resolutions.direct_chat`` when given,
    which is what the chat endpoints surface as
    ``payload["inqtrix"]["model_resolution"]``.
    """
    result_state: dict[str, Any] = {
        "answer": answer,
        "round": 1,
        "queries": ["test query"],
        "all_citations": ["https://example.com/source"],
        "report_references": [
            {"label": "E1", "url": "https://example.com/source", "tier": "unknown"},
        ],
        "final_confidence": 8,
    }
    if model_resolution is not None:
        result_state["node_model_resolutions"] = {"direct_chat": model_resolution}
    return {
        "answer": answer,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "result_state": result_state,
    }


def wait_for_run_status(
    client: TestClient,
    run_id: str,
    status: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Poll the run summary until *status* is reached or fail loudly."""
    deadline = time.time() + timeout
    summary: dict[str, Any] = {}
    while time.time() < deadline:
        summary = client.get(f"/v1/runs/{run_id}").json()
        if summary.get("status") == status:
            return summary
        time.sleep(0.01)
    raise AssertionError(
        f"run {run_id} did not reach status {status!r} within {timeout}s; "
        f"last summary: {summary}"
    )


def parse_sse_frames(body: str) -> list[tuple[str | None, str]]:
    """Split an SSE body into ``(event_name, data_json)`` frames.

    ``event_name`` is ``None`` for frames without an ``event:`` line
    (the OpenAI chat chunk format uses bare ``data:`` frames).
    """
    frames: list[tuple[str | None, str]] = []
    for raw_frame in body.split("\n\n"):
        if not raw_frame.strip():
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in raw_frame.split("\n"):
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        frames.append((event_name, "\n".join(data_lines)))
    return frames
