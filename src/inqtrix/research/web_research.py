"""Algorithm adapters for the existing web-research graph.

``run_web_graph`` / ``run_web_graph_test`` are the registry-side seam
between the platform layer and the LangGraph engine: streamed AND
non-streamed chat, native ``/v1/runs``, and ``/v1/test/run`` all execute
through this module. Tests patch ``inqtrix.research.web_research.run_web_graph``
— the ONE seam for every chat path now that streaming dispatches through the
registry — and the adapters resolve the module global at call time, so late
patching keeps working exactly as before.

Both adapters wrap the SAME graph entry point: the research/direct-LLM
split is carried by ``agent_settings.skip_search`` (resolved by the
application service), and the graph branches internally. The two
algorithm classes exist so the registry, capability manifest, and
result typing can treat the modes as first-class peers — when the
direct-LLM path eventually gets its own slim implementation, only this
module changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from inqtrix.core.algorithms import AlgorithmId
from inqtrix.core.results import AgentResult, RunRequest
from inqtrix.graph import run as run_web_graph
from inqtrix.graph import run_test as run_web_graph_test  # noqa: F401  — /v1/test/run seam

if TYPE_CHECKING:
    from inqtrix.core.context import RunContext, RuntimeContext


def _execute_graph(
    request: RunRequest,
    context: "RunContext",
) -> dict[str, Any]:
    """Invoke the graph with the per-request execution bundle.

    Module-global lookup of ``run_web_graph`` happens here at call
    time — the load-bearing detail that keeps monkeypatched fakes
    effective even when patched after app construction.

    Optional seams (``progress_queue``, ``cancel_event``, ``run_id``,
    ``run_event_sink``, ``web_recency``) are only passed when set, preserving
    the exact historical call shape per protocol path: request/response chat
    passes none of them, the native run worker passes the cancel/event
    trio. Omitting a ``None`` is semantically identical for the graph
    (all optional seams default to ``None``) and keeps strict test fakes that
    pin the per-path signature working.
    """
    optional_kwargs: dict[str, Any] = {}
    if context.progress_queue is not None:
        optional_kwargs["progress_queue"] = context.progress_queue
    if context.cancel_token is not None:
        optional_kwargs["cancel_event"] = context.cancel_token
    if context.run_id is not None:
        optional_kwargs["run_id"] = context.run_id
    if context.event_sink is not None:
        optional_kwargs["run_event_sink"] = context.event_sink
    if context.token_budget > 0:
        optional_kwargs["token_budget"] = context.token_budget
    if request.web_recency is not None:
        optional_kwargs["web_recency"] = request.web_recency
    return run_web_graph(
        request.question,
        history=request.history,
        providers=context.providers,
        strategies=context.strategies,
        settings=context.agent_settings,
        **optional_kwargs,
    )


class WebResearchAlgorithm:
    """Iterative web research (classify -> plan -> search -> evaluate -> answer)."""

    id = AlgorithmId.RESEARCH.value
    display_name = "Web Research"

    def capabilities(self) -> dict:
        """Manifest entry for the capability endpoint and clients.

        ``terminal_node`` is the snapshot node name a completed native
        run reports.
        """
        return {
            "requires": ["llm", "web_search"],
            "streams_events": True,
            "supports_chat_completions": True,
            "terminal_node": "answer",
            "produces": ["answer", "citations", "claims", "sources"],
        }

    def run(
        self,
        request: RunRequest,
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> AgentResult:
        """Execute the research graph and wrap its raw result."""
        from inqtrix.agents.source_policy import require_task_allowed

        require_task_allowed("web_research", policy=request.source_policy)
        raw = _execute_graph(request, context)
        return AgentResult(
            answer=str(raw.get("answer", "")),
            result_type="research_result",
            raw=raw,
        )


class DirectLlmAlgorithm:
    """Direct LLM chat without web search (``skip_search`` path)."""

    id = AlgorithmId.DIRECT_LLM.value
    display_name = "Direct LLM"

    def capabilities(self) -> dict:
        """Manifest entry for the capability endpoint and clients."""
        return {
            "requires": ["llm"],
            "streams_events": True,
            "supports_chat_completions": True,
            "terminal_node": "direct_llm",
            "produces": ["answer"],
        }

    def run(
        self,
        request: RunRequest,
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> AgentResult:
        """Execute the direct-chat branch of the graph."""
        raw = _execute_graph(request, context)
        return AgentResult(
            answer=str(raw.get("answer", "")),
            result_type="direct_llm_result",
            raw=raw,
        )
