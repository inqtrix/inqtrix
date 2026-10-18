"""OTel fundament (INQTRIX_TRACING): setup modes, spool, propagation, envelope.

Tests build their OWN TracerProvider instances (never installing the
process-global one) so the suite stays free of global tracing state;
``setup_tracing``'s install-once contract is verified against a stubbed
global setter.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
)

from inqtrix.observability import otel as otel_module
from inqtrix.observability.context import bind_log_context, reset_log_context
from inqtrix.observability.otel import (
    build_tracer_provider,
    run_execute_span,
    setup_tracing,
)
from inqtrix.observability.propagation import (
    TELEMETRY_FIELD,
    extract_context,
    inject_traceparent,
)
from inqtrix.observability.spool import build_spool_exporter
from inqtrix.runtime_logging import emit_runtime_event, sanitize_event_payload
from inqtrix.server.runs import RunStore
from inqtrix.settings import ObservabilitySettings
from tests._tracing_helpers import everything_a_span_exports


def _settings(mode: str, **overrides) -> SimpleNamespace:
    return SimpleNamespace(
        observability=ObservabilitySettings(tracing=mode, **overrides)
    )


def test_mode_off_builds_nothing():
    assert build_tracer_provider(_settings("off"), service_role="api") is None


def test_score_setup_failure_keeps_the_trace_exporter(monkeypatch, caplog):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    def broken(_settings):
        raise RuntimeError("score client initialization failed")

    monkeypatch.setattr("inqtrix.observability.trace_scores.build_score_processor", broken)
    exporter = InMemorySpanExporter()
    monkeypatch.setattr("opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter", lambda: exporter)
    provider = build_tracer_provider(_settings("otlp"), service_role="api")
    with provider.get_tracer("test").start_as_current_span("still-recorded"):
        pass
    assert provider.force_flush()
    provider.shutdown()
    assert [span.name for span in exporter.get_finished_spans()] == ["still-recorded"]
    assert "Run-score setup failed" in caplog.text


def test_mode_local_yields_valid_span_ids_without_exporters():
    provider = build_tracer_provider(_settings("local"), service_role="api")
    try:
        assert provider is not None
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("probe") as span:
            context = span.get_span_context()
            assert context.is_valid
        # local mode: no span processor may export anything.
        active = provider._active_span_processor._span_processors
        assert not [
            p
            for p in active
            if isinstance(p, (BatchSpanProcessor, SimpleSpanProcessor))
        ]
    finally:
        provider.shutdown()


def test_mode_file_spools_otlp_json_lines(tmp_path):
    provider = build_tracer_provider(
        _settings(
            "file",
            trace_spool_dir=str(tmp_path),
            trace_spool_max_mb=16,
        ),
        service_role="worker",
    )
    try:
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("spooled"):
            pass
        assert provider.force_flush()
        files = list(tmp_path.glob("trace-spool-*.otlp.jsonl"))
        assert len(files) == 1
        line = json.loads(files[0].read_text().strip())
        resource_spans = line["resourceSpans"]
        span_names = [
            span["name"]
            for rs in resource_spans
            for scope in rs["scopeSpans"]
            for span in scope["spans"]
        ]
        assert "spooled" in span_names
        attributes = {
            entry["key"]: entry["value"]
            for rs in resource_spans
            for entry in rs["resource"]["attributes"]
        }
        assert attributes["service.name"] == {
            "stringValue": "inqtrix-worker"
        }
    finally:
        provider.shutdown()


def test_spool_total_cap_reclaims_stale_but_protects_fresh_foreign(tmp_path):
    import os as _os
    import time as _time

    # A FRESH foreign spool file may still be actively written by
    # another process — the cap must never touch it.
    foreign_fresh = tmp_path / "trace-spool-1-aa11bb22-000001.otlp.jsonl"
    foreign_fresh.write_text("x" * 4096)
    # A STALE file (writer exited or idle) is reclaimable regardless of
    # who created it — otherwise dead processes leak disk forever.
    stale = tmp_path / "trace-spool-999-cc33dd44-000001.otlp.jsonl"
    stale.write_text("y" * 4096)
    backdated = _time.time() - 3600
    _os.utime(stale, (backdated, backdated))
    exporter = build_spool_exporter(str(tmp_path), 0)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("test").start_as_current_span("fresh"):
            pass
        assert foreign_fresh.exists()
        assert not stale.exists()
        assert list(tmp_path.glob("trace-spool-*.otlp.jsonl"))
    finally:
        provider.shutdown()


def test_spool_never_appends_into_a_preexisting_lax_mode_file(tmp_path):
    """PID reuse must not route fresh forensic content into a legacy
    world-readable file: the writer creates its slices O_EXCL and skips
    any name that already exists."""
    import os as _os
    import stat as _stat

    exporter = build_spool_exporter(str(tmp_path), 16)
    # Pre-create the EXACT first slice name this writer would pick,
    # with lax permissions (simulates a pre-fix leftover after pid
    # reuse).
    legacy = tmp_path / (
        f"trace-spool-{exporter._pid}-{exporter._token}-000001.otlp.jsonl"
    )
    legacy.write_text("legacy")
    _os.chmod(legacy, 0o644)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("test").start_as_current_span("s"):
            pass
        assert legacy.read_text() == "legacy"
        written = [
            f
            for f in tmp_path.glob("trace-spool-*.otlp.jsonl")
            if f != legacy and f.stat().st_size > 0
        ]
        (slice_file,) = written
        # The point of this test is that the LEGACY world-readable file
        # was not appended to; the fresh slice carries the writer's own
        # mode (owner + group, never other).
        assert _stat.S_IMODE(_os.stat(slice_file).st_mode) == 0o640
    finally:
        provider.shutdown()


def test_spool_is_never_readable_by_other(tmp_path):
    """Spans can carry forensic prompts, so NOTHING outside the owner and
    the group may read them.

    Group access is deliberate, not laxity: containers run as an
    arbitrary non-root UID in group 0 and write through the group bit,
    and the API process reads the worker's slices through the shared
    spool volume. Forcing owner-only would lock the process out of its
    own spool and break the trace export.
    """
    import os as _os
    import stat as _stat

    spool_dir = tmp_path / "traces"
    exporter = build_spool_exporter(str(spool_dir), 16)
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        with provider.get_tracer("test").start_as_current_span("secret"):
            pass
        dir_mode = _stat.S_IMODE(_os.stat(spool_dir).st_mode)
        assert dir_mode & 0o007 == 0, "spool directory reachable by other"
        assert dir_mode & _stat.S_IRWXU == _stat.S_IRWXU
        (slice_file,) = spool_dir.glob("trace-spool-*.otlp.jsonl")
        file_mode = _stat.S_IMODE(_os.stat(slice_file).st_mode)
        assert file_mode & 0o007 == 0, "spool slice readable by other"
        assert file_mode == 0o640
    finally:
        provider.shutdown()


def test_setup_tracing_installs_exactly_once(monkeypatch):
    installed: list[object] = []
    monkeypatch.setattr(
        otel_module, "_state", {"provider": None, "installed": False}
    )
    monkeypatch.setattr(
        otel_trace, "set_tracer_provider", installed.append
    )
    first = setup_tracing(_settings("local"), service_role="api")
    second = setup_tracing(_settings("local"), service_role="api")
    try:
        assert first is not None
        assert second is first
        assert len(installed) == 1
    finally:
        first.shutdown()


def test_setup_tracing_off_touches_nothing(monkeypatch):
    monkeypatch.setattr(
        otel_module, "_state", {"provider": None, "installed": False}
    )
    assert setup_tracing(_settings("off"), service_role="api") is None
    assert otel_module._state["installed"] is False


def test_traceparent_roundtrip_through_payload():
    provider = build_tracer_provider(_settings("local"), service_role="api")
    try:
        tracer = provider.get_tracer("test")
        payload: dict = {"question": "q"}
        with tracer.start_as_current_span("submit") as span:
            inject_traceparent(payload)
            expected_trace_id = span.get_span_context().trace_id
        carrier = payload[TELEMETRY_FIELD]
        assert "traceparent" in carrier
        context = extract_context(payload)
        assert context is not None
        extracted = otel_trace.get_current_span(context).get_span_context()
        assert extracted.trace_id == expected_trace_id
    finally:
        provider.shutdown()


def test_extract_context_tolerates_legacy_payloads():
    assert extract_context(None) is None
    assert extract_context({}) is None
    assert extract_context({"question": "old row"}) is None
    assert extract_context({TELEMETRY_FIELD: "not-a-mapping"}) is None


def test_run_execute_span_is_safe_without_tracing():
    with run_execute_span(
        run_id="run_x", tenant_id="default", attempt=1, payload=None
    ) as span:
        # Non-recording (no global provider installed by the suite),
        # but the context manager itself must never fail.
        assert span is not None


def test_memory_store_submit_persists_traceparent():
    provider = build_tracer_provider(_settings("local"), service_role="api")
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=20,
    )
    try:
        tracer = provider.get_tracer("test")
        with tracer.start_as_current_span("api-request"):
            summary = store.submit(
                question="traced",
                stack_name="default",
                work=lambda handle: None,
                request_payload={"body": {}},
            )
        record = store._records[summary["run_id"]]
        assert "traceparent" in record.request_payload[TELEMETRY_FIELD]
    finally:
        provider.shutdown()


def test_memory_store_trace_id_survives_event_ring_eviction():
    """The admin trace lookup must NOT depend on the bounded SSE replay
    ring: long runs evict old events, the captured field survives."""
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=5,
    )

    def work(handle):
        handle.emit("inqtrix.run.trace", {"trace_id": "a" * 32})
        for index in range(20):  # evicts the trace event from the ring
            handle.emit("phase", {"status": f"step-{index}"})
        handle.emit_answer("done")

    summary = store.submit(
        question="ring", stack_name="default", work=work
    )
    run_id = summary["run_id"]
    import time as _time

    from inqtrix.server.runs import RunStatus

    deadline = _time.time() + 10
    while (
        store._records[run_id].status is not RunStatus.COMPLETED
        and _time.time() < deadline
    ):
        _time.sleep(0.05)
    record = store._records[run_id]
    assert record.status is RunStatus.COMPLETED
    assert all(
        event["type"] != "inqtrix.run.trace" for event in record.events
    )
    assert store.trace_id(run_id) == "a" * 32


def test_sanitize_event_payload_keeps_correlation_envelope():
    sanitized = sanitize_event_payload(
        "run_end",
        {
            "event": "run_end",
            "run_id": "run_1",
            "status": "completed",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
            "request_id": "req-1",
        },
    )
    assert sanitized["trace_id"] == "a" * 32
    assert sanitized["span_id"] == "b" * 16
    assert sanitized["request_id"] == "req-1"


def test_runtime_event_carries_the_request_id(monkeypatch):
    """The event's join key back to the HTTP request.

    Trace and span ids are NOT repeated in the payload — the event
    hangs on its span, which already has both. The request id is the
    one correlation field the span does not carry.
    """
    from inqtrix.observability import otel as otel_module
    from tests._tracing_helpers import ProxyTrace, in_memory_provider

    provider, exporter = in_memory_provider()
    monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    tokens = bind_log_context(request_id="req-99")
    try:
        with provider.get_tracer("test").start_as_current_span("run-segment"):
            emit_runtime_event(
                "run_end", {"run_id": "run_1", "status": "completed"}
            )
    finally:
        reset_log_context(tokens)
        provider.shutdown()

    (span,) = exporter.get_finished_spans()
    (event,) = span.events
    assert event.attributes["request_id"] == "req-99"
    assert "trace_id" not in event.attributes
    assert "span_id" not in event.attributes


def test_span_event_reports_value_truncation(monkeypatch):
    """A capped nested value must raise the
    truncation event — the old 2048-byte cut silently produced invalid
    JSON with no marker anywhere."""
    from inqtrix.observability import otel as otel_module
    from tests._tracing_helpers import ProxyTrace, in_memory_provider

    provider, exporter = in_memory_provider()
    monkeypatch.setattr(otel_module, "_event_truncation_warned", False)
    monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    # Deliberately past the 256 KiB backstop: normal payloads must NOT
    # truncate (that is the contract), so the test has to be pathological
    # to exercise the reporting path at all.
    big = ["cit_" + "x" * 40 for _ in range(8000)]
    with provider.get_tracer("test").start_as_current_span("node"):
        otel_module.add_span_event("claim_merge", {"citation_ids": big})
    spans = exporter.get_finished_spans()
    names = [e.name for e in spans[0].events]
    assert "claim_merge" in names
    assert "inqtrix.truncation" in names
    trunc = next(e for e in spans[0].events if e.name == "inqtrix.truncation")
    from inqtrix.observability import semconv

    assert trunc.attributes[semconv.TRUNCATION_LIMIT_NAME] == (
        "claim_merge.citation_ids"
    )
    assert trunc.attributes[semconv.TRUNCATION_ORIGINAL_SIZE] > (
        trunc.attributes[semconv.TRUNCATION_CAPPED_SIZE]
    )


def test_forensic_without_recording_sink_warns(monkeypatch, caplog):
    """Forensic depth with tracing off/local or
    a sampled-out rate records NOTHING — that must never be silent."""
    from inqtrix.settings import Settings

    monkeypatch.setenv("OBSERVABILITY_PROFILE", "forensic")
    monkeypatch.setenv("INQTRIX_TRACING", "local")
    with caplog.at_level("WARNING", logger="inqtrix"):
        Settings()
    assert any("no spans are recorded" in r.message for r in caplog.records)

    caplog.clear()
    monkeypatch.setenv("INQTRIX_TRACING", "otlp")
    monkeypatch.setenv("INQTRIX_TRACE_SAMPLE_RATE", "0.2")
    with caplog.at_level("WARNING", logger="inqtrix"):
        Settings()
    assert any("head sampling decides per run" in r.message for r in caplog.records)


def test_server_span_makes_the_persisted_payload_carry_the_trace(monkeypatch):
    """The trace chain must hold through the REAL ASGI stack.

    Hand-opening a span around ``store.submit`` proves only that the
    helper works; it cannot detect that no span is active during an
    actual request — in which case ``inject_traceparent`` writes nothing
    and the worker starts a disconnected root span.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from inqtrix.observability.propagation import (
        TELEMETRY_FIELD,
        inject_traceparent,
    )
    from inqtrix.server.request_context import RequestContextMiddleware

    provider = TracerProvider()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: provider)
    captured: dict = {}

    async def _submit(request):
        # Stands in for the run store's submit: it injects whatever
        # trace context the request path made current.
        payload: dict = {"body": {}}
        inject_traceparent(payload)
        captured.update(payload)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/v1/runs", _submit, methods=["POST"])])
    client = TestClient(RequestContextMiddleware(app))
    client.post("/v1/runs", json={})

    assert TELEMETRY_FIELD in captured, (
        "no traceparent persisted — the request path has no active span, "
        "so the worker would open a disconnected root span"
    )
    assert "traceparent" in captured[TELEMETRY_FIELD]


def test_server_span_continues_an_upstream_trace(monkeypatch):
    """An incoming traceparent must be adopted, not replaced."""
    from opentelemetry.sdk.trace import TracerProvider
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from inqtrix.server.request_context import RequestContextMiddleware

    provider = TracerProvider()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: provider)
    seen: dict = {}

    async def _echo(request):
        seen["trace_id"] = otel_trace.get_current_span(

        ).get_span_context().trace_id
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/v1/runs", _echo, methods=["POST"])])
    client = TestClient(RequestContextMiddleware(app))
    upstream = "4bf92f3577b34da6a3ce929d0e0e4736"
    client.post(
        "/v1/runs",
        json={},
        headers={
            "traceparent": f"00-{upstream}-00f067aa0ba902b7-01",
        },
    )
    assert format(seen["trace_id"], "032x") == upstream


def test_probe_paths_are_not_traced(monkeypatch):
    """Liveness/readiness/scrape traffic must not flood the waterfall."""
    from opentelemetry.sdk.trace import TracerProvider
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from inqtrix.server.request_context import RequestContextMiddleware

    provider = TracerProvider()
    monkeypatch.setattr(otel_trace, "get_tracer_provider", lambda: provider)
    recorded: dict = {}

    async def _health(request):
        recorded["recording"] = otel_trace.get_current_span().is_recording()
        return JSONResponse({"status": "ok"})

    app = Starlette(
        routes=[
            Route("/health", _health),
            Route("/internal/collaboration/policy-events", _health),
        ]
    )
    client = TestClient(RequestContextMiddleware(app))
    client.get("/health")
    assert recorded["recording"] is False
    # Service-to-service polling is continuous background chatter; one
    # knowledge run's window carried twice as many of these as the run
    # itself, which would bury every user-initiated trace.
    recorded.clear()
    client.get("/internal/collaboration/policy-events")
    assert recorded["recording"] is False


def _drive_server_span(monkeypatch, status_code: int):
    """Run one request through the middleware and return its finished span.

    The status is only known inside the ASGI ``send`` callback, so this also
    proves the span is still open when the response starts — a span closed
    around ``self.app`` alone could never carry the status at all.

    The tracer is swapped through ``get_tracer`` rather than by installing a
    global provider: a global one is not restorable, and an earlier draft of
    this test poisoned 195 unrelated tests in the same process.
    """
    import asyncio

    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from inqtrix.server.request_context import RequestContextMiddleware

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace, "get_tracer", lambda *args, **kwargs: provider.get_tracer("test")
    )

    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [],
        })
        await send({"type": "http.response.body", "body": b""})

    async def sink(message):
        return None

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/runs",
        "headers": [],
        "client": ("127.0.0.1", 1234),
    }
    asyncio.run(RequestContextMiddleware(app)(scope, lambda: None, sink))
    spans = exporter.get_finished_spans()
    return spans[-1] if spans else None


def test_the_server_span_carries_the_response_status(monkeypatch):
    """Without it a trace showed only method and path, so a rejected request
    read exactly like a successful one and every status question had to be
    answered from the access log instead."""
    from inqtrix.observability import semconv

    span = _drive_server_span(monkeypatch, 404)
    assert span is not None
    assert span.attributes[semconv.HTTP_RESPONSE_STATUS_CODE] == 404
    assert span.attributes[semconv.HTTP_REQUEST_METHOD] == "GET"
    assert span.attributes[semconv.URL_PATH] == "/v1/runs"


def test_only_a_server_fault_marks_the_span_as_an_error(monkeypatch):
    """HTTP semantic conventions: a 4xx is the client's fault, so a SERVER
    span stays UNSET and is found through the status attribute; 5xx is the
    server's own failure and marks the span."""
    from opentelemetry.trace import StatusCode

    assert _drive_server_span(monkeypatch, 200).status.status_code is StatusCode.UNSET
    assert _drive_server_span(monkeypatch, 404).status.status_code is StatusCode.UNSET
    assert _drive_server_span(monkeypatch, 503).status.status_code is StatusCode.ERROR


# --------------------------------------------------------------------- #
# Request span naming: route template, never a path-borne credential
# --------------------------------------------------------------------- #

_GUEST_LINK_TOKEN = "egl1.sentinel-guest-link-token-that-must-not-leak"


def _server_span_exporter(monkeypatch):
    """An in-memory exporter behind the tracer the request middleware uses."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        otel_trace,
        "get_tracer",
        lambda *args, **kwargs: provider.get_tracer("test"),
    )
    return exporter


def _guest_link_route_app():
    """Both public guest-link routes behind the real request middleware and
    a CORS layer, so ``scope["route"]`` has to travel out through another
    middleware before the request span can read it."""
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware

    from inqtrix.server.request_context import RequestContextMiddleware

    app = FastAPI()

    @app.get("/v1/editor/share-links/{token}")
    async def describe(token: str):
        return {"object": "editor_guest_link"}

    @app.post("/v1/editor/share-links/{token}:unlock")
    async def unlock(token: str):
        return {"object": "editor_guest_session"}

    @app.get("/v1/editor/share-links/{token}/failing")
    async def failing(token: str):
        raise RuntimeError(f"lookup failed for /v1/editor/share-links/{token}")

    app.add_middleware(
        CORSMiddleware, allow_origins=["https://desk.example.test"]
    )
    app.add_middleware(RequestContextMiddleware)
    return app


def _request_spans(exporter):
    return [
        span
        for span in exporter.get_finished_spans()
        if span.kind == otel_trace.SpanKind.SERVER
    ]


def test_the_request_span_names_the_route_template_never_the_guest_token(
    monkeypatch,
):
    """A guest-link token in the URL path is a bearer credential. The request
    span was named after the raw path, so every guest request put a live
    token into the trace name and the path attribute."""
    from fastapi.testclient import TestClient

    from inqtrix.observability import semconv

    exporter = _server_span_exporter(monkeypatch)
    response = TestClient(_guest_link_route_app()).get(
        f"/v1/editor/share-links/{_GUEST_LINK_TOKEN}"
    )
    assert response.status_code == 200

    (span,) = _request_spans(exporter)
    assert span.name == "GET /v1/editor/share-links/{token}"
    assert span.attributes[semconv.HTTP_ROUTE] == "/v1/editor/share-links/{token}"
    assert span.attributes[semconv.URL_PATH] == "/v1/editor/share-links/[REDACTED]"
    assert _GUEST_LINK_TOKEN not in everything_a_span_exports(span)


def test_the_unlock_route_is_named_by_its_template(monkeypatch):
    from fastapi.testclient import TestClient

    exporter = _server_span_exporter(monkeypatch)
    response = TestClient(_guest_link_route_app()).post(
        f"/v1/editor/share-links/{_GUEST_LINK_TOKEN}:unlock",
        json={"password": "irrelevant"},
    )
    assert response.status_code == 200

    (span,) = _request_spans(exporter)
    assert span.name == "POST /v1/editor/share-links/{token}:unlock"
    assert _GUEST_LINK_TOKEN not in everything_a_span_exports(span)


def test_an_unmatched_path_is_named_by_its_method_and_its_token_is_scrubbed(
    monkeypatch,
):
    """Without a matched route there is no template; the name falls back to
    the method alone, and the path attribute passes the shared credential
    sanitizer."""
    from fastapi.testclient import TestClient

    from inqtrix.observability import semconv

    exporter = _server_span_exporter(monkeypatch)
    response = TestClient(_guest_link_route_app()).get(
        f"/s/{_GUEST_LINK_TOKEN}"
    )
    assert response.status_code == 404

    (span,) = _request_spans(exporter)
    assert span.name == "GET"
    assert semconv.HTTP_ROUTE not in span.attributes
    assert span.attributes[semconv.URL_PATH] == "/s/[REDACTED]"
    assert _GUEST_LINK_TOKEN not in everything_a_span_exports(span)


def test_an_exception_on_the_request_span_is_recorded_without_the_guest_token(
    monkeypatch,
):
    """An exception that escapes the app is recorded on the request span. Its
    text can repeat the path; the recording keeps the exception's type and
    frames, never its text, and the span still says what failed."""
    from fastapi.testclient import TestClient
    from opentelemetry.trace import StatusCode

    exporter = _server_span_exporter(monkeypatch)
    response = TestClient(
        _guest_link_route_app(), raise_server_exceptions=False
    ).get(f"/v1/editor/share-links/{_GUEST_LINK_TOKEN}/failing")
    assert response.status_code == 500

    (span,) = _request_spans(exporter)
    exported = everything_a_span_exports(span)
    assert _GUEST_LINK_TOKEN not in exported
    assert span.status.status_code is StatusCode.ERROR
    exception_events = [event for event in span.events if event.name == "exception"]
    assert len(exception_events) == 1
    assert exception_events[0].attributes["exception.type"] == "RuntimeError"
    assert exception_events[0].attributes["exception.message"] == "RuntimeError"


def test_an_exception_on_the_request_span_carries_no_more_than_the_log_line(
    monkeypatch,
):
    """A database error that escapes a handler spells out the statement's
    parameters: document text, e-mail addresses. The logs record an
    application exception by type, stable code and frame locations only; the
    request span recorded the full exception text (credentials removed), so
    the trace carried document text and personal data no log line carries.
    The span records the logs' projection: the same type and code, the same
    frames, no prose and no source lines."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opentelemetry.trace import StatusCode
    from sqlalchemy.exc import DataError

    from inqtrix.server.request_context import RequestContextMiddleware

    email = "alice.sentinel@example.org"
    body = "Vertraulicher Absatz SENTINEL-DOCUMENT-BODY"
    app = FastAPI()

    @app.put("/v1/editor/documents/{document_id}")
    async def save(document_id: str):
        raise DataError(
            "UPDATE editor_documents SET content_markdown=$1 WHERE owner_email=$2",
            (body, email),
            Exception('invalid byte sequence for encoding "UTF8": 0x00'),
        )

    app.add_middleware(RequestContextMiddleware)
    exporter = _server_span_exporter(monkeypatch)
    response = TestClient(app, raise_server_exceptions=False).put(
        "/v1/editor/documents/ed_1"
    )
    assert response.status_code == 500

    (span,) = _request_spans(exporter)
    exported = everything_a_span_exports(span)
    assert email not in exported
    assert "SENTINEL-DOCUMENT-BODY" not in exported
    assert "UPDATE editor_documents" not in exported
    assert "invalid byte sequence" not in exported
    # What the span still says, in the words the log line uses.
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "DataError[code=9h9h]"
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.type"] == "sqlalchemy.exc.DataError"
    assert event.attributes["exception.message"] == "DataError[code=9h9h]"
    stacktrace = event.attributes["exception.stacktrace"]
    assert stacktrace.startswith("Traceback (most recent call last):")
    assert ", in save" in stacktrace
    assert stacktrace.rstrip().endswith("DataError[code=9h9h]")


def test_the_app_names_request_spans_by_route_through_its_real_middleware_stack(
    monkeypatch,
):
    """The full application stack between the router and the outermost
    request middleware must hand the matched route back out; a guest-link
    path the in-memory app does not route is named by its method alone."""
    from fastapi.testclient import TestClient

    from inqtrix.providers.base import ProviderContext
    from inqtrix.server.app import create_app
    from inqtrix.settings import AgentSettings, ModelSettings, Settings

    class _NoLLM:
        def is_available(self) -> bool:
            return True

    class _NoSearch:
        def is_available(self) -> bool:
            return True

    exporter = _server_span_exporter(monkeypatch)
    app = create_app(
        settings=Settings(models=ModelSettings(), agent=AgentSettings()),
        providers=ProviderContext(llm=_NoLLM(), search=_NoSearch()),
    )
    with TestClient(app) as client:
        client.get("/v1/runs/run_unknown")
        client.get(f"/v1/editor/share-links/{_GUEST_LINK_TOKEN}")

    run_span, guest_span = _request_spans(exporter)
    assert run_span.name == "GET /v1/runs/{run_id}"
    assert guest_span.name == "GET"
    assert _GUEST_LINK_TOKEN not in everything_a_span_exports(guest_span)
