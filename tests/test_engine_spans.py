"""Engine spans: forensic→span-event bridge, capability spans, event flattening.

The helpers under test resolve the process-global tracer; the shared
``recording`` fixture swaps the module's ``_otel_trace`` for a proxy bound
to a test-local provider, so nothing global is installed and suites stay
isolated.
"""

from __future__ import annotations

import json
import logging

import pytest

from inqtrix.agents.kernel import tools as kernel_tools
from inqtrix.observability.otel import add_span_event, operation_span
from inqtrix.runtime_logging import emit_runtime_event
from tests._tracing_helpers import everything_a_span_exports


def test_add_span_event_flattens_nested_values(recording):
    with operation_span("probe"):
        add_span_event(
            "stop_cascade",
            {
                "confidence": 82,
                "final_done": True,
                "reason": "confidence_stop",
                "scores": {"coverage": 3, "evidence": 5},
                "skipped": None,
            },
        )
    (span,) = recording.get_finished_spans()
    (event,) = [e for e in span.events if e.name == "stop_cascade"]
    attrs = dict(event.attributes)
    assert attrs["confidence"] == 82
    assert attrs["final_done"] is True
    assert attrs["reason"] == "confidence_stop"
    assert json.loads(attrs["scores"]) == {"coverage": 3, "evidence": 5}
    assert "skipped" not in attrs


def test_forensic_events_bridge_onto_the_active_span(recording):
    """The bridge fires even when the log level gate is CLOSED."""
    logger = logging.getLogger("inqtrix")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL)  # log sink off — trace sink stays on
    try:
        with operation_span("evaluate"):
            emit_runtime_event(
                "stop_cascade",
                {
                    "run_id": "run_1",
                    "confidence": 77,
                    "final_done": False,
                    "round": 2,
                },
            )
    finally:
        logger.setLevel(previous_level)
    (span,) = recording.get_finished_spans()
    (event,) = [e for e in span.events if e.name == "stop_cascade"]
    attrs = dict(event.attributes)
    assert attrs["confidence"] == 77
    assert attrs["round"] == 2
    assert attrs["run_id"] == "run_1"


def test_bridge_is_silent_without_active_span(recording):
    logger = logging.getLogger("inqtrix")
    previous_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        emit_runtime_event(
            "stop_cascade", {"run_id": "run_1"}
        )
    finally:
        logger.setLevel(previous_level)
    assert recording.get_finished_spans() == ()


def test_capability_span_is_a_plain_child_without_raw_exception_text(
    recording, monkeypatch
):
    """The kernel tool observation is the ``execute_tool <name>`` span of
    the tool-call middleware; the capability span inside it is a plain
    child span. It keeps the capability id, but carries no GenAI operation
    (which would make it a second tool observation), no arguments even with
    content capture on, and no exception prose: an exception crossing it
    is recorded once, projected, on the tool span."""
    from opentelemetry.trace import StatusCode

    from inqtrix.observability import content as content_module
    from inqtrix.observability.content import ContentCapturePolicy

    monkeypatch.setattr(
        content_module,
        "_active_policy",
        ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
    )
    monkeypatch.setattr(
        kernel_tools,
        "_invoke_capability_inner",
        lambda capability_id, payload: {"ok": True},
    )
    with operation_span("execute_tool web_instant") as outer:
        result = kernel_tools._invoke_capability(
            "web.search.instant", {"query": "MARKER-ARGS"}
        )
    assert result == {"ok": True}
    inner, outer_span = recording.get_finished_spans()
    assert outer_span.name == "execute_tool web_instant"
    assert inner.name == "web.search.instant"
    assert inner.parent.span_id == outer.get_span_context().span_id
    attrs = dict(inner.attributes)
    assert "gen_ai.operation.name" not in attrs
    assert attrs["inqtrix.tool.capability"] == "web.search.instant"
    assert "inqtrix.tool.failure_code" not in attrs
    assert "MARKER-ARGS" not in everything_a_span_exports(inner)

    def _raise(capability_id, payload):
        raise RuntimeError("MARKER-INNER")

    monkeypatch.setattr(kernel_tools, "_invoke_capability_inner", _raise)
    with pytest.raises(RuntimeError, match="MARKER-INNER"):
        kernel_tools._invoke_capability("web.search.instant", {"query": "q"})
    failed = recording.get_finished_spans()[-1]
    assert failed.name == "web.search.instant"
    assert failed.status.status_code is StatusCode.UNSET
    assert not [event for event in failed.events if event.name == "exception"]
    assert "MARKER-INNER" not in everything_a_span_exports(failed)


def test_json_text_content_of_a_dict_stays_valid_and_capped(recording):
    """The tool observation's input and output pass one content path that
    always writes valid JSON within the byte cap: text as a JSON string,
    a structure as its object JSON text, and a structure that does not fit
    as the JSON string of its cut rendering. Every cut raises a truncation
    event naming the attribute; credential keys never reach the span."""
    from inqtrix.observability import content
    from inqtrix.observability.content import ContentCapturePolicy

    policy = ContentCapturePolicy(configured_capture=True, max_attr_bytes=120)
    with operation_span("execute_tool probe") as span:
        content.record_content(
            span,
            "langfuse.observation.input",
            {"content": "x" * 400, "api_key": "SECRET"},
            policy,
        )
        content.record_content(
            span,
            "langfuse.observation.output",
            {"query": "kurz", "api_key": "SECRET"},
            policy,
        )
    with operation_span("execute_tool text") as text_span:
        content.record_content(
            text_span, "langfuse.observation.output", "y" * 400, policy
        )
    structured, text = recording.get_finished_spans()

    cut = structured.attributes["langfuse.observation.input"]
    assert isinstance(json.loads(cut), str)
    assert len(cut.encode("utf-8")) <= 120
    assert json.loads(structured.attributes["langfuse.observation.output"]) == {
        "query": "kurz"
    }
    clipped_text = text.attributes["langfuse.observation.output"]
    # A text is encoded once: the decoded value is the text itself, cut with
    # the visible marker, not a quoted rendering of it.
    decoded = json.loads(clipped_text)
    assert decoded.startswith("yyy") and decoded.endswith("…[truncated]")
    assert len(clipped_text.encode("utf-8")) <= 120
    for span, key in (
        (structured, "langfuse.observation.input"),
        (text, "langfuse.observation.output"),
    ):
        (event,) = [e for e in span.events if e.name == "inqtrix.truncation"]
        assert event.attributes["limit_name"] == key
        assert event.attributes["capped_size"] == len(
            span.attributes[key].encode("utf-8")
        )
        assert event.attributes["original_size"] > 120
        assert "SECRET" not in everything_a_span_exports(span)


def test_knowledge_search_span_names_the_searched_boundary(recording):
    """The searched collection set is traceable without content capture:
    the capability span under the tool observation carries the resolved
    ids and their count as plain attributes (the query stays behind the
    content policy)."""
    import asyncio

    from inqtrix.auth.principal import Principal
    from inqtrix.capabilities.catalog.knowledge import (
        KnowledgeSearchInput,
        build_knowledge_capabilities,
    )
    from inqtrix.capabilities.registry import CapabilityContext, CapabilityError
    from inqtrix.knowledge.stores.ports import VectorStoreUnavailable
    from inqtrix.observability.otel import operation_span

    class _Service:
        async def assert_collections_visible(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return None

        async def search_reported(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise VectorStoreUnavailable("refused")

    (definition,) = [
        d for d in build_knowledge_capabilities(_Service())  # type: ignore[arg-type]
        if d.id == "knowledge.search"
    ]
    context = CapabilityContext(
        principal=Principal(user_id=None, kind="anonymous"),
        knowledge_collection_ids=frozenset({"kc_b", "kc_a"}),
    )
    with operation_span("knowledge.search", {}):
        with pytest.raises(CapabilityError):
            asyncio.run(definition.handler(KnowledgeSearchInput(query="Frist?"), context))
    (span,) = recording.get_finished_spans()
    attrs = dict(span.attributes)
    # Pinned boundary, no explicit ids: the whole boundary, sorted.
    assert attrs["inqtrix.knowledge.scope"] == "pinned"
    assert list(attrs["inqtrix.knowledge.collection_ids"]) == ["kc_a", "kc_b"]
    assert attrs["inqtrix.knowledge.collection_count"] == 2
    assert "Frist" not in str(attrs)
    # An unscoped principal (no pinned boundary, no ids) searches its whole
    # tenant: the span says so instead of claiming zero collections.
    unbounded = CapabilityContext(
        principal=Principal(user_id=None, kind="anonymous"),
        knowledge_collection_ids=None,
    )
    with operation_span("knowledge.search", {}):
        with pytest.raises(CapabilityError):
            asyncio.run(definition.handler(KnowledgeSearchInput(query="Frist?"), unbounded))
    (_, span2) = recording.get_finished_spans()
    attrs2 = dict(span2.attributes)
    assert attrs2["inqtrix.knowledge.scope"] == "unbounded"
    assert "inqtrix.knowledge.collection_ids" not in attrs2
    assert "inqtrix.knowledge.collection_count" not in attrs2


def test_capability_failure_text_marks_the_span(recording, monkeypatch):
    failure = kernel_tools._CapabilityFailureText(
        "nicht verfuegbar", code="capability_denied"
    )
    monkeypatch.setattr(
        kernel_tools,
        "_invoke_capability_inner",
        lambda capability_id, payload: failure,
    )
    result = kernel_tools._invoke_capability("knowledge.search", {})
    assert result is failure
    (span,) = recording.get_finished_spans()
    assert (
        dict(span.attributes)["inqtrix.tool.failure_code"]
        == "capability_denied"
    )


# ------------------------------------------------------------------ #
# Task spans, evidence cuts and node decisions on the trace
# ------------------------------------------------------------------ #


def test_mission_task_gets_a_span_that_carries_the_purpose(
    recording, monkeypatch
):
    """Every mission task opens ``agent.task``; the provider calls it makes
    nest under it and are attributed to the task — the missing half of
    the waterfall (research nodes already had this per node)."""
    from types import SimpleNamespace

    from inqtrix.agents import algorithm as mission
    from inqtrix.agents.control_ports import PlanTaskRecord
    from inqtrix.agents.scheduler import TaskOutcome
    from inqtrix.observability.context import current_purpose

    seen: dict = {}

    def _fake_instant(deps, task):
        purpose = current_purpose()
        seen["purpose"] = purpose.purpose if purpose else None
        seen["task_id"] = purpose.task_id if purpose else None
        with operation_span("probe"):
            pass
        return TaskOutcome(status="completed", summary="ok")

    monkeypatch.setattr(mission, "_run_web_instant", _fake_instant)
    monkeypatch.setattr(mission, "require_task_allowed", lambda *a, **k: None)
    monkeypatch.setattr(mission, "coerce_source_policy", lambda deps: None)
    task = PlanTaskRecord(
        task_id="t3",
        plan_id="plan_x",
        run_id="run_x",
        ordinal=2,
        title="Aktuellen Leitzins ermitteln",
        tool_kind="web_instant",
    )
    deps = SimpleNamespace(emit=lambda *_args, **_kw: None)
    outcome = mission._execute_task(deps, {}, task, attempt=1)
    assert outcome.status == "completed"
    assert seen == {"purpose": "web_instant", "task_id": "t3"}
    spans = {span.name: span for span in recording.get_finished_spans()}
    task_span = spans["agent.task"]
    attrs = dict(task_span.attributes)
    assert attrs["inqtrix.task_id"] == "t3"
    assert attrs["inqtrix.task_kind"] == "web_instant"
    assert attrs["inqtrix.attempt"] == 1
    probe = spans["probe"]
    assert probe.parent is not None
    assert probe.parent.span_id == task_span.get_span_context().span_id


def test_evidence_cut_is_visible_on_the_node_span(recording, monkeypatch):
    """A budget cut is an attribute, a WARNING level and a named event on
    the node span — the trace-side twin of the prompt footer. The
    reviewed Gruendlich run lost 13 of 30 sources this way and reported
    a false negative; nothing in the trace said so."""
    from inqtrix.evidence import EvidenceOverview
    from inqtrix.nodes import _trace_evidence_overview
    from inqtrix.observability import content as content_module
    from inqtrix.observability.content import ContentCapturePolicy

    monkeypatch.setattr(
        content_module,
        "_active_policy",
        ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768),
    )

    overview = EvidenceOverview(
        markdown="RESEARCH RESULT R1 ...",
        rendered_record_count=17,
        omitted_record_count=2,
        omitted_records=[
            {
                "evidence_id": "ev_a",
                "label": "E21",
                "url": "https://example.gv.at/a",
                "query_id": "qry_at",
                "reason": "whole_group",
            },
            {
                "evidence_id": "ev_b",
                "label": "E22",
                "url": "https://example.gv.at/b",
                "query_id": "qry_at",
                "reason": "whole_group",
            },
        ],
        omitted_query_ids=["qry_at"],
    )
    with operation_span("evaluate"):
        _trace_evidence_overview(overview, budget_chars=24000, view="answer")
    (span,) = recording.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.evidence.rendered_count"] == 17
    assert attrs["inqtrix.evidence.omitted_count"] == 2
    assert attrs["inqtrix.evidence.omitted_query_count"] == 1
    assert attrs["inqtrix.evidence.budget_chars"] == 24000
    assert attrs["langfuse.observation.level"] == "WARNING"
    (event,) = [e for e in span.events if e.name == "inqtrix.evidence.omitted"]
    event_attrs = dict(event.attributes)
    assert event_attrs["omitted_count"] == 2
    assert json.loads(event_attrs["labels"]) == ["E21", "E22"]
    assert json.loads(event_attrs["query_ids"]) == ["qry_at"]
    # URLs are content: absent under the summary policy ...
    assert "urls" not in event_attrs
    # ... and present only when content capture is on.
    monkeypatch.setattr(
        content_module,
        "_active_policy",
        ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768),
    )
    with operation_span("evaluate"):
        _trace_evidence_overview(overview, budget_chars=24000, view="answer")
    forensic_span = recording.get_finished_spans()[-1]
    (forensic_event,) = [
        e for e in forensic_span.events if e.name == "inqtrix.evidence.omitted"
    ]
    assert json.loads(dict(forensic_event.attributes)["urls"]) == [
        "https://example.gv.at/a",
        "https://example.gv.at/b",
    ]


def test_uncut_evidence_leaves_no_warning_on_the_node_span(recording):
    from inqtrix.evidence import EvidenceOverview
    from inqtrix.nodes import _trace_evidence_overview

    with operation_span("answer"):
        _trace_evidence_overview(
            EvidenceOverview(markdown="x", rendered_record_count=5),
            budget_chars=30000,
            view="answer",
        )
    (span,) = recording.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.evidence.omitted_count"] == 0
    assert "langfuse.observation.level" not in attrs
    assert not [e for e in span.events if e.name == "inqtrix.evidence.omitted"]


def test_node_span_exports_decisions_and_flattened_counts(recording):
    """Strings and booleans that name a decision (stop reason, contract,
    evidence-contract status, done) and the count dicts reach the node
    span, not only bare numeric snapshot keys — otherwise the evaluate
    decision would live only as prose in the prompt."""
    from inqtrix.graph import _set_node_span_attributes

    result = {
        "_stop_reason": "max_rounds",
        "answer_contract": "claim_check",
        "evidence_contract_status": "needs_review",
        "done": True,
        "source_role_counts": {"strategy": 2, "model": 9, "unjudged": 1, "conflicts": 0},
    }
    snapshot = {
        "confidence": 6,
        "aspect_coverage": 0.667,
        "done": True,
        "claim_status_counts": {"verified": 1, "contested": 0, "unverified": 6},
        "source_tier_counts": {"primary": 1, "unknown": 22},
        "current_node": "evaluate",
        "last_message": "",
    }
    with operation_span("evaluate") as span:
        _set_node_span_attributes(span, "evaluate", result, snapshot)
    (finished,) = recording.get_finished_spans()
    attrs = dict(finished.attributes)
    assert attrs["inqtrix.confidence"] == 6
    assert attrs["inqtrix.aspect_coverage"] == 0.667
    assert attrs["inqtrix.claim_status_counts.verified"] == 1
    assert attrs["inqtrix.claim_status_counts.unverified"] == 6
    assert attrs["inqtrix.source_tier_counts.unknown"] == 22
    assert attrs["inqtrix.stop_reason"] == "max_rounds"
    assert attrs["inqtrix.answer_contract"] == "claim_check"
    assert attrs["inqtrix.evidence_contract_status"] == "needs_review"
    assert attrs["inqtrix.evaluate.done"] is True
    assert attrs["inqtrix.source_roles.model"] == 9 and attrs["inqtrix.source_roles.unjudged"] == 1
    # Strings from the snapshot are still not exported blindly.
    assert "inqtrix.current_node" not in attrs


def test_research_node_binds_its_name_as_purpose(recording, monkeypatch):
    """Every research node bound through the graph carries its name as
    the purpose of the provider calls it makes — the research nodes call
    providers directly, so the graph binding is their only attribution."""
    from types import SimpleNamespace

    from inqtrix import nodes as nodes_module
    from inqtrix.graph import default_graph_config
    from inqtrix.observability.context import current_purpose
    from inqtrix.settings import AgentSettings
    from inqtrix.state import initial_state

    seen: dict = {}

    def _fake_classify(state, *, providers, strategies, settings):
        purpose = current_purpose()
        seen["purpose"] = purpose.purpose if purpose else None
        return state

    monkeypatch.setattr(nodes_module, "classify", _fake_classify)
    config = default_graph_config(
        providers=SimpleNamespace(),
        strategies=SimpleNamespace(),
        settings=AgentSettings(),
    )
    state = initial_state("Was ist passiert?", max_total_seconds=30)
    config.nodes["classify"](state)
    assert seen == {"purpose": "classify"}
    assert current_purpose() is None, "the binding must not leak past the node"
    spans = {span.name: span for span in recording.get_finished_spans()}
    assert dict(spans["classify"].attributes)["inqtrix.node"] == "classify"


def test_claim_extraction_binds_its_own_purpose_inside_the_search_node(
    recording,
):
    """Claim extraction runs on the search node's pool threads with its own
    model; it must be attributed as ``claim_extract`` while inheriting the
    task the enclosing node was bound to."""
    from queue import Queue
    from types import SimpleNamespace

    from inqtrix.nodes import search
    from inqtrix.observability.context import bind_purpose, current_purpose
    from inqtrix.providers.base import ProviderContext
    from inqtrix.search_result import GroundedSearchResult, GroundedSource
    from inqtrix.settings import AgentSettings
    from inqtrix.state import initial_state
    from inqtrix.strategies import StrategyContext, create_default_strategies

    class _Search:
        def search(self, *a, **kw):
            return GroundedSearchResult(
                answer="Gefundener Text",
                sources=[GroundedSource(url="https://example.com/report", rank=1)],
            )

        def consume_nonfatal_notice(self):
            return None

        def is_available(self):
            return True

    class _LLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *a, **kw):
            return ""

        def consume_nonfatal_notice(self):
            return None

        def is_available(self):
            return True

    seen: list[tuple[str | None, str | None]] = []

    class _RecordingExtraction:
        def extract(self, *a, **kw):
            purpose = current_purpose()
            seen.append(
                (purpose.purpose, purpose.task_id) if purpose else (None, None)
            )
            return ([], 0, 0)

        def consume_nonfatal_notice(self):
            return None

    settings = AgentSettings(first_round_queries=1, max_rounds=4)
    llm = _LLM()
    defaults = create_default_strategies(
        settings, llm=llm, claim_extract_model="claim-extract-model"
    )
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_RecordingExtraction(),
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state(
        "Was ist passiert?", progress_queue=Queue(), max_total_seconds=30
    )
    state["queries"] = ["q1"]
    with bind_purpose("search", task_id="t9"):
        search(
            state,
            providers=ProviderContext(llm=llm, search=_Search()),
            strategies=strategies,
            settings=settings,
        )
    assert seen == [("claim_extract", "t9")]


def test_node_span_exports_the_contract_source_coverage_source_and_event_mode(recording):
    """The classify and evaluate decisions are trace filters: who
    set the contract (model or fallback), who judged the coverage, and
    whether the run is in event mode."""
    from inqtrix.graph import _set_node_span_attributes

    result = {
        "done": False,
        "answer_contract": "comparison",
        "answer_contract_source": "llm",
        "aspects_source": "llm",
        "evaluate_status": "insufficient",
        "coverage_source": "llm",
        "event_mode": False,
    }
    with operation_span("evaluate") as span:
        _set_node_span_attributes(span, "evaluate", result, {"round": 1})
    attrs = dict(recording.get_finished_spans()[0].attributes)
    assert attrs["inqtrix.answer_contract"] == "comparison"
    assert attrs["inqtrix.answer_contract_source"] == "llm"
    assert attrs["inqtrix.aspects_source"] == "llm"
    assert attrs["inqtrix.evaluate_status"] == "insufficient"
    assert attrs["inqtrix.coverage_source"] == "llm"
    assert attrs["inqtrix.event_mode"] is False
