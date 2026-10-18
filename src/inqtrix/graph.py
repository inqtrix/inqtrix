"""LangGraph state machine construction and entry points.

Provides :func:`run` (production) and :func:`run_test` (testing) as the
two public entry points for executing the research agent pipeline.

The graph topology is:

    classify --> (done? --> answer) | plan
    plan     --> (done? --> answer) | search
    search   --> evaluate
    evaluate --> (done? --> answer) | plan
    answer   --> END
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import partial
from queue import Queue
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from inqtrix.core.results import WebRecency
from inqtrix.exceptions import (
    AgentCancelled,
    AgentRateLimited,
    AgentTokenBudgetExceeded,
)
from inqtrix.i18n import t
from inqtrix.observability import semconv
from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.prompt_language import (
    OUTPUT_FORMAT_BLOCK,
    WRITING_STYLE_BLOCK,
    directive_for,
    resolve_answer_language,
)
from inqtrix.observability.context import bind_purpose

from inqtrix.observability.otel import operation_span
from inqtrix.providers.base import ProviderContext
from inqtrix.result import ResearchResult, ResearchResultExportOptions
from inqtrix.runtime_logging import log_run_end, log_run_start
from inqtrix.scoring import claim_assessments
from inqtrix.settings import AgentSettings
from inqtrix.state import (
    AgentState,
    build_run_snapshot,
    check_cancel_event,
    emit_progress,
    emit_run_event,
    initial_state,
)
from inqtrix.strategies import StrategyContext

log = logging.getLogger("inqtrix")

_FLATTENED_COUNT_KEYS = frozenset({"source_tier_counts", "claim_status_counts"})
"""Snapshot dicts exported one level flat onto the node span."""

_DECISION_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("_stop_reason", semconv.INQTRIX_STOP_REASON),
    ("answer_contract", semconv.INQTRIX_ANSWER_CONTRACT),
    ("answer_contract_source", semconv.INQTRIX_ANSWER_CONTRACT_SOURCE),
    ("aspects_source", semconv.INQTRIX_ASPECTS_SOURCE),
    ("evaluate_status", semconv.INQTRIX_EVALUATE_STATUS),
    ("coverage_source", semconv.INQTRIX_COVERAGE_SOURCE),
    ("evidence_contract_status", semconv.INQTRIX_EVIDENCE_CONTRACT_STATUS),
)
"""State keys (string decisions) exported verbatim onto every node span."""


def _set_node_span_attributes(
    node_span: Any,
    node_name: str,
    result: AgentState,
    finished_snapshot: dict[str, Any],
) -> None:
    """Export the node's decision as span attributes (always on).

    Derived counters only (the snapshot contract already excludes raw
    ledgers/provider payloads): every numeric snapshot key, the
    count dicts flattened one level (``inqtrix.claim_status_counts.
    verified``), and the few strings/booleans that name a decision —
    stop reason, done, contract, evidence-contract status. Without
    them the evaluate decision exists only as prose in the prompt,
    so a question like "why did round 2 run?" needs a dump instead
    of a trace filter.
    """
    for key, value in finished_snapshot.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if isinstance(value, dict) and key in _FLATTENED_COUNT_KEYS:
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (int, float)) and not isinstance(
                        sub_value, bool
                    ):
                        node_span.set_attribute(
                            f"inqtrix.{key}.{sub_key}", sub_value
                        )
            continue
        node_span.set_attribute(f"inqtrix.{key}", value)
    node_span.set_attribute(f"inqtrix.{node_name}.done", bool(result.get("done", False)))
    if "event_mode" in result:
        node_span.set_attribute(semconv.INQTRIX_EVENT_MODE, bool(result.get("event_mode")))
    role_counts = result.get("source_role_counts")
    if isinstance(role_counts, dict):
        # How the run's sources were placed: structural floor, extractor
        # judgement, unjudged, and judgements that conflicted with the
        # pinned one — the trace answer to "why is this source unknown?".
        for sub_key, sub_value in role_counts.items():
            if isinstance(sub_value, int) and not isinstance(sub_value, bool):
                node_span.set_attribute(
                    f"{semconv.INQTRIX_SOURCE_ROLES_PREFIX}{sub_key}", sub_value
                )
    for state_key, attribute in _DECISION_ATTRIBUTES:
        value = result.get(state_key)
        if value is None or value == "":
            continue
        node_span.set_attribute(attribute, str(value))


# ------------------------------------------------------------------ #
# Graph configuration
# ------------------------------------------------------------------ #


@dataclass
class GraphConfig:
    """Declarative description of the agent graph topology.

    *nodes* maps logical node names to callables ``(AgentState) -> AgentState``.
    *edges* is a list of unconditional ``(source, target)`` pairs.
    *conditional_edges* is a list of ``(source, router_fn)`` pairs where the
    router returns the next node name.
    *entry_point* is the first node to execute.
    """

    nodes: dict[str, Callable] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    conditional_edges: list[tuple[str, Callable]] = field(default_factory=list)
    entry_point: str = "classify"


def default_graph_config(
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> GraphConfig:
    """Create the standard pipeline :class:`GraphConfig`.

    Uses :func:`functools.partial` to bind *providers*, *strategies*, and
    *settings* to each node function so LangGraph sees the expected
    ``(AgentState) -> AgentState`` signature.
    """
    from inqtrix.nodes import answer, classify, evaluate, plan, search

    def _bind(node_name: str, fn: Callable, s: AgentState) -> AgentState:
        s["_current_node"] = node_name
        emit_run_event(
            s,
            "inqtrix.node.started",
            {
                "node": node_name,
                "snapshot": build_run_snapshot(s, current_node=node_name),
            },
        )
        # One span per graph node: the LLM/search call spans (provider
        # wrappers) and the forensic span events (stop cascade, answer
        # sections, failures) nest under it — the trace waterfall shows
        # the round structure without touching the run_events surface.
        with operation_span(
            node_name,
            {
                semconv.INQTRIX_NODE: node_name,
                semconv.INQTRIX_ROUND: int(s.get("round", 0) or 0),
            },
        ) as node_span:
            try:
                # The node name is the purpose of every provider call the
                # node makes (classify, plan, search, evaluate, answer): the
                # research nodes call providers directly, not through the
                # structured-call helper, so this is their one binding.
                with bind_purpose(node_name):
                    result = fn(
                        s,
                        providers=providers,
                        strategies=strategies,
                        settings=settings,
                    )
            except Exception as exc:
                if isinstance(exc, AgentCancelled) and exc.partial_state is None:
                    exc.partial_state = s
                emit_run_event(
                    s,
                    "inqtrix.node.failed",
                    {
                        "node": node_name,
                        "snapshot": build_run_snapshot(
                            s, current_node=node_name
                        ),
                    },
                )
                raise
            result["_current_node"] = node_name
            finished_snapshot = build_run_snapshot(
                result, current_node=node_name
            )
            if node_span is not None:
                _set_node_span_attributes(
                    node_span, node_name, result, finished_snapshot
                )
        emit_run_event(
            result,
            "inqtrix.node.finished",
            {
                "node": node_name,
                "snapshot": finished_snapshot,
            },
        )
        return result

    return GraphConfig(
        nodes={
            "classify": partial(_bind, "classify", classify),
            "plan": partial(_bind, "plan", plan),
            "search": partial(_bind, "search", search),
            "evaluate": partial(_bind, "evaluate", evaluate),
            "answer": partial(_bind, "answer", answer),
        },
        edges=[
            ("search", "evaluate"),
            ("answer", END),
        ],
        conditional_edges=[
            ("classify", lambda s: "answer" if s["done"] else "plan"),
            ("plan", lambda s: "answer" if s["done"] else "search"),
            ("evaluate", lambda s: "answer" if s["done"] else "plan"),
        ],
        entry_point="classify",
    )


# ------------------------------------------------------------------ #
# Graph compilation
# ------------------------------------------------------------------ #


def build_graph(config: GraphConfig) -> Any:
    """Compile a :class:`~langgraph.graph.StateGraph` from *config*.

    Returns the compiled graph object ready for ``.invoke()``.
    """
    g = StateGraph(AgentState)

    for name, fn in config.nodes.items():
        g.add_node(name, fn)

    g.set_entry_point(config.entry_point)

    for src, dst in config.edges:
        g.add_edge(src, dst)

    for src, router in config.conditional_edges:
        g.add_conditional_edges(src, router)

    return g.compile()


_GRAPH_CACHE_MAXSIZE = 32
_GraphCacheKey = tuple[int, int, str]
_graph_cache: OrderedDict[_GraphCacheKey, Any] = OrderedDict()
_graph_cache_lock = threading.RLock()


def _settings_fingerprint(settings: AgentSettings) -> str:
    """Return a stable value fingerprint for graph-affecting settings."""
    payload = settings.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def get_agent(
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> Any:
    """Build and return the compiled agent graph.

    The compiled graph is cached and reused as long as the same *providers*
    and *strategies* objects are paired with equivalent *settings* values.
    The bounded cache is protected by a lock so concurrent HTTP requests
    cannot observe a graph compiled for a different settings variant.
    """
    key: _GraphCacheKey = (
        id(providers),
        id(strategies),
        _settings_fingerprint(settings),
    )
    with _graph_cache_lock:
        cached = _graph_cache.get(key)
        if cached is not None:
            _graph_cache.move_to_end(key)
            return cached

    config = default_graph_config(providers, strategies, settings)
    agent = build_graph(config)

    with _graph_cache_lock:
        cached = _graph_cache.get(key)
        if cached is not None:
            _graph_cache.move_to_end(key)
            return cached
        _graph_cache[key] = agent
        while len(_graph_cache) > _GRAPH_CACHE_MAXSIZE:
            _graph_cache.popitem(last=False)

    return agent


# ------------------------------------------------------------------ #
# Public entry points
# ------------------------------------------------------------------ #


_DIRECT_CHAT_SYSTEM_PROMPT = (
    "You are a helpful conversational assistant without web research. "
    "Answer the question clearly and precisely from your training knowledge. "
    "When you do not know something reliably, say so honestly. "
    "Do not invent sources and do not add citations.\n\n"
    + OUTPUT_FORMAT_BLOCK
    + "\n\n"
    + WRITING_STYLE_BLOCK
    + "\nKeep answers compact; prefer inline math."
)


def _format_direct_chat_prompt(
    question: str, history: str, answer_language: str | None = None
) -> str:
    """Build the prompt sent to the LLM in skip-search mode.

    The answer-language directive leads the prompt (phrased in the target
    language; the system prompt is written in one language and a model
    drifts back to it otherwise). History is appended verbatim when
    present so the provider sees the same conversational context the UI
    already passes to the server.
    """
    directive = directive_for(answer_language)
    if history:
        return (
            f"{directive}\n\n"
            f"Conversation so far:\n{history}\n\n"
            f"Current question:\n{question}"
        )
    return f"{directive}\n\n{question}"


def _run_direct_chat(
    question: str,
    history: str,
    *,
    progress_queue: Queue | None,
    providers: ProviderContext,
    settings: AgentSettings,
    cancel_event: threading.Event | None,
    run_id: str | None = None,
    run_event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    token_budget: int = 0,
) -> dict[str, Any]:
    """Execute the skip-search short-circuit: LLM-only, no graph.

    Emits two progress markers (``chat_only.start`` / ``chat_only.done``)
    so a progress UI still has something to show. The
    returned ``result_state`` mimics the shape of the research path so
    downstream callers behave identically, minus the research-specific
    fields.
    """
    state = initial_state(
        question,
        history,
        progress_queue,
        max_total_seconds=settings.max_total_seconds,
        cancel_event=cancel_event,
        max_rounds=settings.max_rounds,
        run_id=run_id,
        run_event_sink=run_event_sink,
        token_budget=token_budget,
    )
    log_run_start(
        question=question,
        history=history,
        providers=providers,
        settings=settings,
        run_mode="run_chat_only",
        run_id=state.get("_run_id"),
    )

    state["_current_node"] = "direct_llm"
    emit_progress(state, "chat_only.start")
    t0 = time.monotonic()

    answer_language = resolve_answer_language(question)
    log.info(
        "answer language resolved: %s (source=%s)",
        answer_language.code or "unresolved",
        answer_language.source,
    )
    prompt = _format_direct_chat_prompt(question, history, answer_language.code)
    from inqtrix.nodes import _resolve_node_llm

    direct_chat_model, direct_chat_effort = _resolve_node_llm(
        state, settings, providers, "direct_chat"
    )
    try:
        # The direct path bypasses the node loop that binds every research
        # node's purpose, so the one provider call here binds its own:
        # without it the chat generation is an anonymous row in the trace.
        with bind_purpose("direct_chat"):
            llm_response = providers.llm.complete_with_metadata(
                prompt,
                system=_DIRECT_CHAT_SYSTEM_PROMPT,
                model=direct_chat_model,
                reasoning_effort=direct_chat_effort,
                timeout=settings.reasoning_timeout,
                state=state,
                deadline=state.get("deadline"),
            )
        # The provider records usage on the shared state. Direct mode has no
        # next graph node, so this explicit terminal boundary is what enforces
        # both a late client cancel and the server-owned token ceiling.
        check_cancel_event(state)
    except AgentCancelled as exc:
        log.info(
            "Chat-only run stopped (error_type=%s)",
            type(exc).__name__,
        )
        state["cancelled"] = True
        state["cancel_reason"] = (
            "token_budget_exceeded"
            if isinstance(exc, AgentTokenBudgetExceeded)
            else "client_requested_cancel"
        )
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run_chat_only",
            status="cancelled",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason=state["cancel_reason"],
        )
        return {
            "answer": "",
            "usage": {
                "prompt_tokens": state.get("total_prompt_tokens", 0),
                "completion_tokens": state.get("total_completion_tokens", 0),
            },
            "result_state": state,
        }
    except AgentRateLimited as exc:
        emit_progress(state, t(state, "run_aborted", exc=exc))
        log.error(
            "ABORT (chat-only, error_type=%s)",
            type(exc).__name__,
        )
        state["_terminal_failure"] = {
            "type": "rate_limited",
            "message": str(exc),
        }
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run_chat_only",
            status="aborted",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason="rate_limited",
        )
        return {
            "answer": t(state, "chat_rate_limited_answer", error=exc),
            "usage": {
                "prompt_tokens": state.get("total_prompt_tokens", 0),
                "completion_tokens": state.get("total_completion_tokens", 0),
            },
            "result_state": state,
        }
    except Exception as exc:
        emit_progress(state, t(state, "chat_mode_failed", exc=exc))
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run_chat_only",
            status="error",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason=type(exc).__name__,
        )
        raise

    answer_text = (
        getattr(llm_response, "content", None)
        or getattr(llm_response, "text", None)
        or str(llm_response)
    )
    # The same currency guard as the agent answers: "$5 und $10" in prose
    # must not become inline math in the renderer.
    answer_text = normalize_agent_markdown(answer_text)
    state["answer"] = answer_text
    state["done"] = True
    emit_progress(state, "chat_only.done")

    elapsed = time.monotonic() - t0
    log.info("Chat-only finished in %.2fs", elapsed)
    log_run_end(
        run_id=state.get("_run_id"),
        run_mode="run_chat_only",
        status="success",
        elapsed_s=elapsed,
        state=state,
        reason="chat_only_done",
    )

    return {
        "answer": answer_text,
        "usage": {
            "prompt_tokens": state.get("total_prompt_tokens", 0),
            "completion_tokens": state.get("total_completion_tokens", 0),
        },
        "result_state": state,
    }


def run(
    question: str,
    history: str = "",
    progress_queue: Queue | None = None,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
    cancel_event: threading.Event | None = None,
    run_id: str | None = None,
    run_event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    token_budget: int = 0,
    web_recency: WebRecency | None = None,
) -> dict[str, Any]:
    """Execute the research agent and return answer + usage + state.

    Parameters
    ----------
    question:
        The user question to research.
    history:
        Optional conversation history for context.
    progress_queue:
        Optional :class:`~queue.Queue` receiving ``("progress", msg)`` tuples.
    providers:
        The active LLM / search providers.
    strategies:
        The active strategy implementations.
    settings:
        Agent behaviour settings.
    web_recency:
        Optional provider-neutral recency filter supplied by a delegating
        agent. When set, it overrides classifier inference.

    Returns
    -------
    dict with keys ``answer``, ``usage``, ``result_state``.
    """
    if len(question) > settings.max_question_length:
        return {
            "answer": (
                t(
                    {"language": resolve_answer_language(question).code},
                    "question_too_long",
                    limit=settings.max_question_length,
                )
            ),
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "result_state": {},
        }

    # "Chat ohne Websuche"-Kurzschluss: wenn der Caller
    # ``skip_search=True`` setzt, umgehen wir den kompletten
    # classify/plan/search/evaluate/answer-Graphen und rufen direkt den
    # LLM-Provider mit Frage + History. Nützlich für UI-Clients, die
    # einen reinen Konversationsmodus anbieten wollen.
    if settings.skip_search:
        return _run_direct_chat(
            question,
            history,
            progress_queue=progress_queue,
            providers=providers,
            settings=settings,
            cancel_event=cancel_event,
            run_id=run_id,
            run_event_sink=run_event_sink,
            token_budget=token_budget,
        )

    agent = get_agent(providers, strategies, settings)

    state = initial_state(
        question,
        history,
        progress_queue,
        max_total_seconds=settings.max_total_seconds,
        cancel_event=cancel_event,
        max_rounds=settings.max_rounds,
        run_id=run_id,
        run_event_sink=run_event_sink,
        token_budget=token_budget,
        web_recency=web_recency,
    )
    log_run_start(
        question=question,
        history=history,
        providers=providers,
        settings=settings,
        run_mode="run",
        run_id=state.get("_run_id"),
    )

    t0 = time.monotonic()
    try:
        result = agent.invoke(state)
        state = result
        # The answer node is terminal and therefore has no successor whose
        # entry check could observe usage added by the final model call.
        check_cancel_event(state)
    except AgentCancelled as exc:
        # Implicit-cancel-on-disconnect path: the SSE client went away,
        # the streaming layer set the event, and the next node-boundary
        # probe raised. Return a marked-cancelled state instead of
        # propagating; the streaming generator already stopped emitting.
        if exc.partial_state is not None:
            state = dict(exc.partial_state)
        log.info("Run stopped (error_type=%s)", type(exc).__name__)
        state["cancelled"] = True
        state["cancel_reason"] = (
            "token_budget_exceeded"
            if isinstance(exc, AgentTokenBudgetExceeded)
            else "client_requested_cancel"
        )
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run",
            status="cancelled",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason=state["cancel_reason"],
        )
        return {
            "answer": "",
            "usage": {
                "prompt_tokens": state.get("total_prompt_tokens", 0),
                "completion_tokens": state.get("total_completion_tokens", 0),
            },
            "result_state": state,
        }
    except AgentRateLimited as exc:
        emit_progress(state, t(state, "run_aborted", exc=exc))
        log.error("ABORT (error_type=%s)", type(exc).__name__)
        state["_terminal_failure"] = {
            "type": "rate_limited",
            "message": str(exc),
        }
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run",
            status="aborted",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason="rate_limited",
        )
        return {
            "answer": t(state, "research_rate_limited_answer", error=exc),
            "usage": {
                "prompt_tokens": state.get("total_prompt_tokens", 0),
                "completion_tokens": state.get("total_completion_tokens", 0),
            },
            "result_state": state,
        }
    except Exception as exc:
        emit_progress(state, t(state, "run_failed", exc=exc))
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run",
            status="error",
            elapsed_s=time.monotonic() - t0,
            state=state,
            reason=type(exc).__name__,
        )
        raise

    elapsed = time.monotonic() - t0
    log.info("Agent finished in %.1fs", elapsed)
    log_run_end(
        run_id=result.get("_run_id", state.get("_run_id")),
        run_mode="run",
        status="success",
        elapsed_s=elapsed,
        state=result,
    )

    return {
        "answer": result["answer"],
        "usage": {
            "prompt_tokens": result.get("total_prompt_tokens", 0),
            "completion_tokens": result.get("total_completion_tokens", 0),
        },
        "result_state": result,
    }


# ------------------------------------------------------------------ #
# Test entry point
# ------------------------------------------------------------------ #


def _build_test_metrics(
    data: dict[str, Any], elapsed: float, **extra: Any
) -> dict[str, Any]:
    """Build the metrics dict for :func:`run_test` (success and error paths)."""
    iter_logs = data.get("iteration_logs", [])
    eval_logs = [e for e in iter_logs if e.get("node") == "evaluate"]
    return {
        "total_rounds": data.get("round", 0),
        "total_elapsed_s": round(elapsed, 2),
        "final_confidence": data.get("final_confidence", 0),
        "total_queries": len(data.get("queries", [])),
        "total_citations": len(data.get("all_citations", [])),
        "total_context_blocks": len(data.get("context", [])),
        "competing_events_detected": (
            bool(data.get("competing_events", ""))
            or any(bool(e.get("competing_events", "")) for e in eval_logs)
        ),
        "stagnation_detected": any(
            e.get("stagnation_detected", False) for e in eval_logs
        ),
        "total_prompt_tokens": data.get("total_prompt_tokens", 0),
        "total_completion_tokens": data.get("total_completion_tokens", 0),
        "falsification_triggered": bool(data.get("falsification_triggered", False)),
        "evidence_consistency_final": data.get("evidence_consistency", 0),
        "evidence_sufficiency_final": data.get("evidence_sufficiency", 0),
        "source_quality_score_final": data.get("source_quality_score", 0.0),
        "source_tier_counts_final": data.get("source_tier_counts", {}),
        "claim_quality_score_final": data.get("claim_quality_score", 0.0),
        "claim_status_counts_final": data.get("claim_status_counts", {}),
        "consolidated_claims_count_final": len(
            claim_assessments(data)
        ),
        "claim_needs_primary_total_final": data.get(
            "claim_needs_primary_total", 0
        ),
        "claim_needs_primary_supported_final": data.get(
            "claim_needs_primary_supported", 0
        ),
        "aspect_coverage_final": data.get("aspect_coverage", 0.0),
        "uncovered_aspects_count_final": len(
            data.get("uncovered_aspects", [])
        ),
        "risk_score": data.get("risk_score", 0),
        "high_risk": bool(data.get("high_risk", False)),
        "utility_scores": data.get("utility_scores", []),
        "utility_stop_triggered": any(
            e.get("utility_stop", False) for e in eval_logs
        ),
        "plateau_stop_triggered": any(
            e.get("plateau_stop", False) for e in eval_logs
        ),
        **extra,
    }


def _build_test_result_export(
    answer: str, data: dict[str, Any], *, source_tiering: Any = None
) -> dict[str, Any]:
    """Project internal state into the public result view for parity surfaces."""
    result = ResearchResult.from_raw(
        {
            "answer": answer,
            "usage": {
                "prompt_tokens": data.get("total_prompt_tokens", 0),
                "completion_tokens": data.get("total_completion_tokens", 0),
            },
            "result_state": data,
        },
        source_tiering=source_tiering,
    )
    return result.to_export_payload(
        ResearchResultExportOptions(
            include_metrics=False,
        )
    )


def run_test(
    question: str,
    *,
    providers: ProviderContext,
    strategies: StrategyContext,
    settings: AgentSettings,
) -> dict[str, Any]:
    """Execute the research agent in testing mode.

    Returns a structured dict with answer, metrics, and iteration logs.
    Only meaningful when ``settings.testing_mode`` is ``True`` (otherwise
    ``iteration_logs`` will be empty).
    """
    if len(question) > settings.max_question_length:
        return {
            "answer": (
                t(
                    {"language": resolve_answer_language(question).code},
                    "question_too_long",
                    limit=settings.max_question_length,
                )
            ),
            "metrics": {},
            "iteration_logs": [],
            "top_sources": [],
            "top_claims": [],
        }

    agent = get_agent(providers, strategies, settings)
    state = initial_state(
        question,
        max_total_seconds=settings.max_total_seconds,
        max_rounds=settings.max_rounds,
    )
    log_run_start(
        question=question,
        history="",
        providers=providers,
        settings=settings,
        run_mode="run_test",
        run_id=state.get("_run_id"),
    )

    t0 = time.monotonic()
    try:
        result = agent.invoke(state)
    except AgentRateLimited as exc:
        log.error("ABORT (test, error_type=%s)", type(exc).__name__)
        elapsed = time.monotonic() - t0
        log_run_end(
            run_id=state.get("_run_id"),
            run_mode="run_test",
            status="aborted",
            elapsed_s=elapsed,
            state=state,
            reason="rate_limited",
        )
        return {
            **_build_test_result_export(f'ABORT: {exc}', state, source_tiering=strategies.source_tiering),
            "metrics": _build_test_metrics(state, elapsed, error=str(exc)),
            "iteration_logs": state.get("iteration_logs", []),
        }

    elapsed = time.monotonic() - t0
    log_run_end(
        run_id=result.get("_run_id", state.get("_run_id")),
        run_mode="run_test",
        status="success",
        elapsed_s=elapsed,
        state=result,
    )
    return {
        **_build_test_result_export(result["answer"], result, source_tiering=strategies.source_tiering),
        "metrics": _build_test_metrics(result, elapsed),
        "iteration_logs": result.get("iteration_logs", []),
    }
