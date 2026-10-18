"""Run-score attribution, absence semantics and nonblocking publication."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from inqtrix.observability.trace_scores import (
    build_score_processor,
    run_score_attributes,
    score_bodies,
)
from inqtrix.settings import ObservabilitySettings, Settings


def _spans(attributes):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span(
        "inqtrix.run", attributes=attributes
    ):
        pass
    provider.shutdown()
    return exporter.get_finished_spans()


def _attributes(**extra):
    return {
        "inqtrix.scores_ready": True,
        "inqtrix.scores_trace_root": True,
        "inqtrix.score.final_confidence": 7,
        **extra,
    }


def test_research_scores_keep_measured_zero_and_omit_absence():
    assert run_score_attributes(
        "research", {"final_confidence": 0, "_stop_reason": "plateau"}
    ) == {
        "inqtrix.score.final_confidence": 0,
        "inqtrix.score.stop_reason": "plateau",
    }
    assert run_score_attributes("research", {}) == {}


@pytest.mark.parametrize("reason", ["confidence_stop", "deadline_exceeded"])
def test_stop_reason_survives_graph_channels_into_run_scores(reason):
    from langgraph.graph import END, StateGraph
    from inqtrix.state import AgentState

    graph = StateGraph(AgentState)
    graph.add_node("evaluate", lambda _: {"_stop_reason": reason, "done": True})

    def answer(state):
        assert state["_stop_reason"] == reason
        return {"answer": "Result"}

    graph.add_node("answer", answer)
    graph.set_entry_point("evaluate")
    graph.add_edge("evaluate", "answer")
    graph.add_edge("answer", END)
    final = graph.compile().invoke({"question": "A factual question"})
    assert run_score_attributes("research", final) == {"inqtrix.score.stop_reason": reason}


def test_deep_scores_preserve_the_submitted_judgement(monkeypatch):
    from inqtrix.observability.trace_scores import record_deep_review_scores

    recorded = []
    monkeypatch.setattr("inqtrix.observability.otel.enrich_current_span", recorded.append)
    record_deep_review_scores(SimpleNamespace(findings=[{}, {}], complete=False,
                                             grounded=True, contradictions_named=False))
    assert recorded == [{"inqtrix.score.deep_review_findings": 2,
                         "inqtrix.score.deep_review_complete": False,
                         "inqtrix.score.deep_review_grounded": True,
                         "inqtrix.score.deep_review_contradictions_named": False}]


@pytest.mark.parametrize("mode", ["workspace_agent", "agent_kernel", "knowledge"])
def test_nonresearch_modes_never_export_unmeasured_research_defaults(mode):
    attrs = run_score_attributes(
        mode,
        {
            "final_confidence": 0,
            "claim_quality_score": 0,
            "critic_report": {"verdict": "revise"},
        },
    )
    assert attrs == (
        {"inqtrix.score.critic_verdict": "revise"} if mode == "workspace_agent" else {}
    )


def test_scores_use_native_types_and_stable_trace_attribution():
    spans = _spans(
        _attributes(
            **{
                "inqtrix.score.stop_reason": "plateau",
                "inqtrix.score.deep_review_grounded": False,
            }
        )
    )
    bodies = score_bodies(spans)
    assert bodies == score_bodies(spans)
    by_name = {body["name"]: body for body in bodies}
    assert {
        key: (body["dataType"], body["value"]) for key, body in by_name.items()
    } == {
        "final_confidence": ("NUMERIC", 7.0),
        "stop_reason": ("CATEGORICAL", "plateau"),
        "deep_review_grounded": ("BOOLEAN", 0.0),
    }
    assert len({body["id"] for body in bodies}) == 3
    assert all(
        body["traceId"] == format(spans[0].context.trace_id, "032x") for body in bodies
    )
    assert all("observationId" not in body for body in bodies)


def test_child_score_is_attached_to_its_execution_observation():
    spans = _spans(_attributes(**{"inqtrix.scores_trace_root": False}))
    (body,) = score_bodies(spans)
    assert body["observationId"] == format(spans[0].context.span_id, "016x")
    other = _spans(_attributes(**{"inqtrix.scores_trace_root": False}))
    assert score_bodies(other)[0]["id"] != body["id"]


@pytest.mark.parametrize("mode", ["run", "deep"])
def test_score_recording_failure_does_not_escape(monkeypatch, caplog, mode):
    from inqtrix.observability import trace_scores

    def broken(_):
        raise RuntimeError("recorder failure")

    monkeypatch.setattr(trace_scores, "_record_warning", False)
    monkeypatch.setattr("inqtrix.observability.otel.enrich_current_span", broken)
    if mode == "run":
        trace_scores.record_run_scores("research", {"final_confidence": 7}, trace_root=True)
    else:
        trace_scores.record_deep_review_scores(SimpleNamespace(findings=[], complete=True, grounded=True, contradictions_named=True))
    assert "could not be recorded" in caplog.text


@pytest.mark.parametrize("ready", [None, False])
def test_unfinished_segments_never_publish_final_scores(ready):
    attrs = _attributes()
    if ready is None:
        del attrs["inqtrix.scores_ready"]
    else:
        attrs["inqtrix.scores_ready"] = ready
    assert score_bodies(_spans(attrs)) == []


def _settings(mode="otlp", enabled=True):
    return Settings(
        observability=ObservabilitySettings(
            INQTRIX_TRACING=mode, INQTRIX_TRACE_SCORES=enabled
        )
    )


@pytest.mark.parametrize(
    "mode,enabled", [("off", True), ("file", True), ("local", True), ("otlp", False)]
)
def test_score_export_is_inactive_without_a_live_enabled_sink(mode, enabled, monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://lf.example/api/public/otel/v1/traces")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Basic%20test")

    def unexpected_client(**_):
        pytest.fail("Inactive score export attempted to create a client")

    monkeypatch.setattr(httpx, "Client", unexpected_client)
    assert build_score_processor(_settings(mode, enabled)) is None


@pytest.mark.parametrize(
    "endpoint,auth",
    [
        ("https://collector.example/v1/traces", "Basic test"),
        ("https://lf.example/api/public/otel/v1/traces", ""),
    ],
)
def test_unusable_score_sink_warns_at_setup(monkeypatch, caplog, endpoint, auth):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=" + auth)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
    assert build_score_processor(_settings()) is None
    assert "Run scores disabled" in caplog.text


@pytest.mark.parametrize("status", [200, 503])
def test_score_requests_run_in_export_thread_and_failure_is_loud(
    monkeypatch, caplog, status
):
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "10")
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        "https://lf.example/api/public/otel/v1/traces",
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "Authorization=Basic%20test"
    )
    calls = []
    closed = []
    sent = threading.Event()
    owner_thread = threading.get_ident()

    def post(url, *, json, headers):
        calls.append((url, json, headers, threading.get_ident()))
        sent.set()
        return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_: SimpleNamespace(post=post, close=lambda: closed.append(True)),
    )
    processor = build_score_processor(_settings())
    provider = TracerProvider()
    provider.add_span_processor(processor)
    with provider.get_tracer("test").start_as_current_span(
        "inqtrix.run", attributes=_attributes()
    ):
        pass
    with provider.get_tracer("test").start_as_current_span("ordinary"):
        pass
    # Explicit force_flush can export on its caller; first wait for the
    # normal background path whose nonblocking contract this test owns.
    assert sent.wait(2)
    assert provider.force_flush()
    provider.shutdown()
    assert len(calls) == 1
    url, body, headers, thread_id = calls[0]
    assert url == "https://lf.example/api/public/scores"
    assert headers == {"Authorization": "Basic test"}
    assert (body["name"], body["dataType"], body["value"]) == (
        "final_confidence",
        "NUMERIC",
        7.0,
    )
    assert thread_id != owner_thread
    assert bool(closed)
    assert ("Run-score export failed" in caplog.text) == (status == 503)
