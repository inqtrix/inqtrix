"""Trace export sources (Langfuse/Spool) and the retention job."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from inqtrix.observability.trace_readers import (
    LangfuseReader,
    SpoolReader,
    TraceExportUnavailable,
    build_trace_reader,
    langfuse_auth_header,
    langfuse_base_url,
)
from inqtrix.observability.trace_retention import prune_old_traces

TRACE_HEX = "0af7651916cd43dd8448eb211c80319c"
TRACE_B64 = base64.b64encode(bytes.fromhex(TRACE_HEX)).decode("ascii")


# --- URL/auth derivation ------------------------------------------------- #


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    (
        ("http://langfuse:3000/api/public/otel", "http://langfuse:3000"),
        (
            "https://langfuse.example/api/public/otel/v1/traces",
            "https://langfuse.example",
        ),
        ("https://langfuse.example/api/public/otel/", "https://langfuse.example"),
        ("", None),
        ("   ", None),
        # Foreign OTLP targets have no Langfuse REST API — they must
        # map to None so callers raise their actionable config error.
        ("http://otel-collector:4318", None),
        ("http://otel-collector:4318/v1/traces", None),
        ("https://tempo.example/otlp", None),
    ),
)
def test_langfuse_base_url(endpoint, expected):
    assert langfuse_base_url(endpoint) == expected


def test_langfuse_auth_header_parses_otlp_headers(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS",
        "Authorization=Basic cGs6c2s=,x-langfuse-ingestion-version=4",
    )
    assert langfuse_auth_header() == "Basic cGs6c2s="
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    assert langfuse_auth_header() is None


def test_langfuse_auth_header_matches_exporter_semantics(monkeypatch):
    """Credential parity with the OTLP exporter: the signal-specific
    variable wins, and percent-encoded values are decoded."""
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic generic"
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
        "Authorization=Basic%20cGs6c2s=",
    )
    assert langfuse_auth_header() == "Basic cGs6c2s="


# --- LangfuseReader ------------------------------------------------------ #


def _response(status, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body or {})


def test_langfuse_reader_uses_v3_detail_endpoint():
    calls = []

    def fake_get(url, params=None, headers=None):
        calls.append(url)
        assert headers["Authorization"] == "Basic abc"
        return _response(
            200,
            {
                "id": TRACE_HEX,
                "htmlPath": f"/project/p1/traces/{TRACE_HEX}",
                "observations": [{"id": "obs1", "input": "hi"}],
            },
        )

    reader = LangfuseReader(
        base_url="http://langfuse:3000", authorization="Basic abc", get=fake_get
    )
    export = reader.get_trace("run_1", TRACE_HEX)
    assert calls == [f"http://langfuse:3000/api/public/traces/{TRACE_HEX}"]
    assert export.source == "langfuse"
    assert export.html_path == f"/project/p1/traces/{TRACE_HEX}"
    assert export.payload["observations"][0]["id"] == "obs1"
    document = export.as_document()
    assert document["run_id"] == "run_1"
    assert document["trace_id"] == TRACE_HEX


def test_langfuse_reader_falls_back_to_v2_cursor_pagination_on_gone():
    """The v2 observations endpoint is CURSOR-paginated (limit+cursor,
    meta.cursor while more rows exist) — never page/totalPages."""

    def fake_get(url, params=None, headers=None):
        if "/api/public/traces/" in url:
            return _response(404)
        assert url.endswith("/api/public/v2/observations")
        assert "page" not in params
        assert params["limit"] == 100
        cursor = params.get("cursor")
        if cursor is None:
            return _response(
                200,
                {"data": [{"id": "obs1"}], "meta": {"cursor": "c2"}},
            )
        assert cursor == "c2"
        return _response(200, {"data": [{"id": "obs2"}], "meta": {}})

    reader = LangfuseReader(
        base_url="http://langfuse:3000", authorization="Basic abc", get=fake_get
    )
    export = reader.get_trace("run_1", TRACE_HEX)
    assert [o["id"] for o in export.payload["observations"]] == [
        "obs1",
        "obs2",
    ]


def test_langfuse_reader_guards_degenerate_success_bodies():
    reader_empty = LangfuseReader(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        get=lambda url, params=None, headers=None: _response(200, {}),
    )
    with pytest.raises(TraceExportUnavailable, match="with no content"):
        reader_empty.get_trace("run_1", TRACE_HEX)

    def broken_json(url, params=None, headers=None):
        def _raise():
            raise ValueError("not json")

        return SimpleNamespace(status_code=200, json=_raise)

    reader_html = LangfuseReader(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        get=broken_json,
    )
    with pytest.raises(TraceExportUnavailable, match="JSON"):
        reader_html.get_trace("run_1", TRACE_HEX)


def test_langfuse_reader_raises_clearly_on_server_error():
    reader = LangfuseReader(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        get=lambda url, params=None, headers=None: _response(503),
    )
    with pytest.raises(TraceExportUnavailable, match="HTTP 503"):
        reader.get_trace("run_1", TRACE_HEX)


# --- SpoolReader ---------------------------------------------------------- #


def _spool_line(trace_b64, name):
    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "scope": {"name": "inqtrix.worker"},
                            "spans": [
                                {"traceId": trace_b64, "name": name},
                                {"traceId": "b3RoZXI=", "name": "noise"},
                            ],
                        }
                    ],
                }
            ]
        }
    )


def test_spool_reader_filters_to_the_requested_trace(tmp_path):
    spool = tmp_path / "trace-spool-1-aa-000001.otlp.jsonl"
    spool.write_text(
        _spool_line(TRACE_B64, "inqtrix.run")
        + "\n"
        + _spool_line("b3RoZXI=", "unrelated")
        + "\nnot-json\n"
    )
    export = SpoolReader(directory=str(tmp_path)).get_trace(
        "run_1", TRACE_HEX
    )
    assert export.source == "spool"
    assert export.payload["replayable"] is True
    assert export.payload["span_count"] == 1
    (line,) = export.payload["lines"]
    (resource_span,) = line["resourceSpans"]
    (scope_span,) = resource_span["scopeSpans"]
    (span,) = scope_span["spans"]
    assert span["name"] == "inqtrix.run"


def test_spool_reader_reports_missing_trace_and_missing_dir(tmp_path):
    with pytest.raises(TraceExportUnavailable, match="No trace spool directory"):
        SpoolReader(directory=str(tmp_path / "missing")).get_trace(
            "run_1", TRACE_HEX
        )
    (tmp_path / "trace-spool-1-aa-000001.otlp.jsonl").write_text(
        _spool_line("b3RoZXI=", "unrelated") + "\n"
    )
    with pytest.raises(TraceExportUnavailable, match=TRACE_HEX):
        SpoolReader(directory=str(tmp_path)).get_trace("run_1", TRACE_HEX)


# --- Source selection ------------------------------------------------------ #


def _settings(mode, spool_dir="logs/traces"):
    return SimpleNamespace(
        observability=SimpleNamespace(
            tracing=mode,
            trace_spool_dir=spool_dir,
            trace_retention_days=30,
        )
    )


def test_build_trace_reader_per_mode(monkeypatch, tmp_path):
    with pytest.raises(TraceExportUnavailable, match="No trace sink"):
        build_trace_reader(_settings("off"))
    with pytest.raises(TraceExportUnavailable, match="No trace sink"):
        build_trace_reader(_settings("local"))
    reader = build_trace_reader(_settings("file", str(tmp_path)))
    assert isinstance(reader, SpoolReader)

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", raising=False)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://langfuse:3000/api/public/otel"
    )
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic abc"
    )
    live = build_trace_reader(_settings("otlp"))
    assert isinstance(live, LangfuseReader)
    assert live.base_url == "http://langfuse:3000"

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    with pytest.raises(TraceExportUnavailable, match="Langfuse"):
        build_trace_reader(_settings("otlp"))


# --- Retention job --------------------------------------------------------- #


def test_prune_old_traces_lists_pages_and_batches_deletes():
    listed_pages = []
    deleted_batches = []

    def fake_get(url, params=None, headers=None):
        assert params["toTimestamp"].endswith("Z")
        listed_pages.append(params["page"])
        if params["page"] == 1:
            return _response(
                200,
                {
                    "data": [{"id": f"t{i}"} for i in range(100)],
                    "meta": {"totalPages": 2},
                },
            )
        return _response(
            200,
            {
                "data": [{"id": "t100"}],
                "meta": {"totalPages": 2},
            },
        )

    def fake_delete(url, *, json, headers):
        deleted_batches.append(list(json["traceIds"]))
        return _response(200, {"success": True})

    report = prune_old_traces(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        retention_days=30,
        get=fake_get,
        delete=fake_delete,
        now=datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc),
    )
    assert listed_pages == [1, 2]
    assert report.cutoff_iso.startswith("2026-06-24T12:00:00")
    assert report.scanned == 101
    assert report.delete_requested == 101
    assert report.failed_batches == 0
    assert [len(b) for b in deleted_batches] == [100, 1]


def test_prune_old_traces_survives_http_failures():
    report = prune_old_traces(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        retention_days=30,
        get=lambda url, params=None, headers=None: _response(500),
        delete=lambda url, *, json, headers: _response(200),
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    assert report.failed_batches == 1
    assert report.delete_requested == 0

    def get_one(url, params=None, headers=None):
        return _response(
            200, {"data": [{"id": "t1"}], "meta": {"totalPages": 1}}
        )

    report = prune_old_traces(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        retention_days=30,
        get=get_one,
        delete=lambda url, *, json, headers: _response(429),
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    assert report.delete_requested == 0
    assert report.failed_batches == 1


def test_prune_old_traces_excludes_already_requested_ids():
    """Langfuse deletes async: a drain-loop re-list returns the same
    ids — the exclusion set must skip them so passes reach FRESH rows."""

    def fake_get(url, params=None, headers=None):
        return _response(
            200,
            {
                "data": [{"id": "t-old"}, {"id": "t-new"}],
                "meta": {"totalPages": 1},
            },
        )

    deleted: list[list[str]] = []

    def fake_delete(url, *, json, headers):
        deleted.append(list(json["traceIds"]))
        return _response(200)

    report = prune_old_traces(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        retention_days=30,
        get=fake_get,
        delete=fake_delete,
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
        exclude=frozenset({"t-old"}),
    )
    assert report.requested_ids == ("t-new",)
    assert deleted == [["t-new"]]


def test_retention_thread_starts_only_for_otlp_with_positive_days(
    monkeypatch,
):
    import threading

    import inqtrix.worker.__main__ as worker_main

    started: list[str] = []

    class _FakeThread:
        def __init__(self, *, target, name, daemon):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    stop = threading.Event()

    worker_main._start_trace_retention_thread(
        _settings("file"), stop
    )
    assert started == []

    otlp = _settings("otlp")
    otlp.observability.trace_retention_days = 0
    worker_main._start_trace_retention_thread(otlp, stop)
    assert started == []

    otlp.observability.trace_retention_days = 30
    worker_main._start_trace_retention_thread(otlp, stop)
    assert started == ["inqtrix-trace-retention"]


def test_prune_old_traces_noop_when_nothing_expired():
    report = prune_old_traces(
        base_url="http://langfuse:3000",
        authorization="Basic abc",
        retention_days=30,
        get=lambda url, params=None, headers=None: _response(
            200, {"data": [], "meta": {"totalPages": 1}}
        ),
        delete=lambda url, *, json, headers: pytest.fail(
            "no delete expected"
        ),
        now=datetime(2026, 7, 24, tzinfo=timezone.utc),
    )
    assert report.scanned == 0
    assert report.delete_requested == 0 and report.failed_batches == 0
