"""Trace export sources: Langfuse OR the file spool, one interface.

The admin export endpoint asks THIS module for the full trace of a run,
and the Baukasten decides where it comes from:

* ``INQTRIX_TRACING=otlp`` → :class:`LangfuseReader` — plain httpx
  against the Langfuse REST API, authenticated with the SAME project
  keys the OTLP exporter uses (parsed from
  ``OTEL_EXPORTER_OTLP_HEADERS``, base URL derived from
  ``OTEL_EXPORTER_OTLP_ENDPOINT``). No second credential set.
* ``INQTRIX_TRACING=file`` → :class:`SpoolReader` — filters the
  OTLP-JSON spool lines by trace id; the result stays REPLAYABLE
  (`python -m inqtrix.observability.replay`) because it keeps the
  ``ExportTraceServiceRequest`` shape.
* ``off`` / ``local`` → :class:`TraceExportUnavailable` with a clear
  operator message (no sink records span details in these modes).

Langfuse contract notes (verified against v3 source): the v3 detail
endpoint ``GET /api/public/traces/{id}`` returns the trace INCLUDING all
observations with input/output in one call, plus ``htmlPath`` for the
"Trace öffnen" deep link. Those v3 trace endpoints are deprecated in
favour of v4, so :meth:`LangfuseReader.get_trace` keeps the
``/api/public/v2/observations`` cursor path as the prepared fallback
codepath.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("inqtrix")


class TraceExportUnavailable(Exception):
    """No trace source can serve — carries the operator-facing reason."""


@dataclass(frozen=True)
class TraceExport:
    """One exported trace, source-native payload plus envelope fields."""

    run_id: str
    trace_id: str
    source: str  # "langfuse" | "spool"
    payload: dict[str, Any]
    # Langfuse deep-link path (e.g. "/project/<id>/traces/<traceId>");
    # the API layer joins it with INQTRIX_TRACE_UI_URL.
    html_path: str | None = None

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "source": self.source,
            "payload": self.payload,
        }
        if self.html_path:
            document["html_path"] = self.html_path
        return document


def langfuse_base_url(otlp_endpoint: str) -> str | None:
    """Derive the Langfuse host base URL from the OTLP endpoint.

    ``https://host/api/public/otel`` (optionally with ``/v1/traces``)
    → ``https://host``. Returns ``None`` for empty AND for foreign
    endpoints: only a URL that actually targets Langfuse's OTLP path
    (``…/api/public/otel``) identifies a Langfuse REST API. A generic
    collector/Tempo/Grafana endpoint must map to ``None`` so callers
    raise their actionable config error instead of firing REST calls at
    a host that will never answer them.
    """
    trimmed = (otlp_endpoint or "").strip().rstrip("/")
    if not trimmed:
        return None
    if trimmed.endswith("/v1/traces"):
        trimmed = trimmed[: -len("/v1/traces")].rstrip("/")
    if not trimmed.endswith("/api/public/otel"):
        return None
    trimmed = trimmed[: -len("/api/public/otel")].rstrip("/")
    return trimmed or None


def _headers_from_otlp_env() -> dict[str, str]:
    """Parse the OTel headers env pair exactly like the OTLP exporter.

    Credential parity means matching the exporter's TWO behaviours: the
    signal-specific ``OTEL_EXPORTER_OTLP_TRACES_HEADERS`` takes
    precedence over the generic variable, and values are percent-
    DECODED (the spec allows ``Basic%20<b64>``; the SDK unquotes it, so
    this side must too or the same config yields a 401 here only).
    """
    from urllib.parse import unquote

    raw = os.getenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "") or os.getenv(
        "OTEL_EXPORTER_OTLP_HEADERS", ""
    )
    headers: dict[str, str] = {}
    for pair in raw.split(","):
        name, separator, value = pair.strip().partition("=")
        if separator and name:
            headers[name.strip()] = unquote(value.strip())
    return headers


def langfuse_auth_header() -> str | None:
    """The Authorization value the OTLP exporter uses, if configured."""
    for name, value in _headers_from_otlp_env().items():
        if name.lower() == "authorization":
            return value
    return None


@dataclass
class LangfuseReader:
    """Reads one full trace from the Langfuse REST API.

    ``get`` is ``httpx.Client.get``-shaped and injectable for tests;
    the default performs a real request with a bounded timeout.
    """

    base_url: str
    authorization: str
    get: Callable[..., Any] | None = None

    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if self.get is not None:
            return self.get(
                url,
                params=params,
                headers={"Authorization": self.authorization},
            )
        import httpx

        with httpx.Client(timeout=30.0) as client:
            return client.get(
                url,
                params=params,
                headers={"Authorization": self.authorization},
            )

    def get_trace(self, run_id: str, trace_id: str) -> TraceExport:
        """Fetch the trace detail (v3), falling back to v2 observations.

        The v3 detail endpoint delivers trace + ALL observations with
        input/output in one call. Because those endpoints are marked
        deprecated upstream, a 404/410 answer switches to the prepared
        v2 codepath: trace core from the list endpoint + observations
        via cursor pagination.
        """
        detail_url = f"{self.base_url}/api/public/traces/{trace_id}"
        response = self._get(detail_url)
        status = getattr(response, "status_code", 0)
        if 200 <= status < 300:
            payload = self._json_dict(response, trace_id)
            if not payload.get("id"):
                # A 200 without a trace body (proxy interception, odd
                # deployment) must not become an audited empty export.
                raise TraceExportUnavailable(
                    f'Langfuse returned no trace body for {trace_id} (HTTP 200 with no content).'
                )
            return TraceExport(
                run_id=run_id,
                trace_id=trace_id,
                source="langfuse",
                payload=payload,
                html_path=str(payload.get("htmlPath") or "") or None,
            )
        if status in (404, 410):
            return self._get_trace_v2(run_id, trace_id)
        raise TraceExportUnavailable(
            f'Langfuse returned HTTP {status} for trace {trace_id}.'
        )

    @staticmethod
    def _json_dict(response: Any, trace_id: str) -> dict[str, Any]:
        """Body as dict, or a clear 409 instead of an escaping 500."""
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001 — proxies return HTML pages
            raise TraceExportUnavailable(
                f'Langfuse returned a non-JSON response for trace {trace_id}.'
            ) from exc
        if not isinstance(body, dict):
            raise TraceExportUnavailable(
                f'Langfuse returned an unexpected response shape for trace {trace_id}.'
            )
        return body

    def _get_trace_v2(self, run_id: str, trace_id: str) -> TraceExport:
        """v4-era codepath: /api/public/v2/observations, CURSOR-paginated.

        Verified against the Langfuse source: the v2 endpoint takes
        ``limit`` + ``cursor`` (no ``page``) and answers with
        ``meta.cursor`` while more rows exist. On the pinned v3 images
        it additionally sits behind a preview opt-in and 404s — that
        surfaces as the clear error below until the backend actually
        moves to v4, which is exactly when this path takes over.
        """
        observations: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(1_000):  # backstop against a cursor loop
            params: dict[str, Any] = {"traceId": trace_id, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            response = self._get(
                f"{self.base_url}/api/public/v2/observations",
                params=params,
            )
            status = getattr(response, "status_code", 0)
            if not 200 <= status < 300:
                raise TraceExportUnavailable(
                    f'Langfuse returned HTTP {status} for the observations of trace {trace_id}.'
                )
            body = self._json_dict(response, trace_id)
            observations.extend(body.get("data") or [])
            cursor = str((body.get("meta") or {}).get("cursor") or "")
            if not cursor:
                break
        if not observations:
            raise TraceExportUnavailable(
                f'Langfuse has no observations for trace {trace_id}; they may have been deleted or not yet ingested.'
            )
        return TraceExport(
            run_id=run_id,
            trace_id=trace_id,
            source="langfuse",
            payload={"id": trace_id, "observations": observations},
        )


def _trace_id_to_proto_b64(trace_id_hex: str) -> str:
    """Proto-JSON encodes span/trace ids as base64 of the raw bytes."""
    return base64.b64encode(bytes.fromhex(trace_id_hex)).decode("ascii")


@dataclass
class SpoolReader:
    """Filters the OTLP-JSON spool for one trace (``file`` mode, no backend).

    The result keeps the ``ExportTraceServiceRequest`` line shape under
    ``payload.lines`` so the exported document can be written back to a
    ``.otlp.jsonl`` file and replayed into Langfuse or any OTLP tool.
    """

    directory: str
    _glob: str = field(default="trace-spool-*.otlp.jsonl", repr=False)

    def get_trace(self, run_id: str, trace_id: str) -> TraceExport:
        wanted = _trace_id_to_proto_b64(trace_id)
        matched_lines: list[dict[str, Any]] = []
        span_count = 0
        spool_dir = Path(self.directory)
        if not spool_dir.is_dir():
            raise TraceExportUnavailable(
                f'No trace spool directory at {self.directory}; check whether any data was recorded with INQTRIX_TRACING=file.'
            )
        for spool_file in sorted(spool_dir.glob(self._glob)):
            try:
                text = spool_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    continue
                filtered = _filter_request(request, wanted)
                if filtered is not None:
                    matched_lines.append(filtered)
                    span_count += _count_spans(filtered)
        if not matched_lines:
            raise TraceExportUnavailable(
                f'Trace {trace_id} is not in the spool at {self.directory}; check retention rotation and the selected process spool.'
            )
        return TraceExport(
            run_id=run_id,
            trace_id=trace_id,
            source="spool",
            payload={
                "format": "otlp-json-lines",
                "replayable": True,
                "span_count": span_count,
                "lines": matched_lines,
            },
        )


def _filter_request(
    request: dict[str, Any], wanted_trace_b64: str
) -> dict[str, Any] | None:
    """Keep only the spans of the wanted trace, or None if none match."""
    kept_resources = []
    for resource_span in request.get("resourceSpans") or []:
        kept_scopes = []
        for scope_span in resource_span.get("scopeSpans") or []:
            kept = [
                span
                for span in scope_span.get("spans") or []
                if span.get("traceId") == wanted_trace_b64
            ]
            if kept:
                kept_scopes.append({**scope_span, "spans": kept})
        if kept_scopes:
            kept_resources.append(
                {**resource_span, "scopeSpans": kept_scopes}
            )
    if not kept_resources:
        return None
    return {"resourceSpans": kept_resources}


def _count_spans(request: dict[str, Any]) -> int:
    return sum(
        len(scope_span.get("spans") or [])
        for resource_span in request.get("resourceSpans") or []
        for scope_span in resource_span.get("scopeSpans") or []
    )


def build_trace_reader(settings: Any) -> LangfuseReader | SpoolReader:
    """Pick the export source for the configured tracing mode.

    Raises :class:`TraceExportUnavailable` with an actionable message
    when the mode records nothing or the otlp target is not a
    reachable/parsable Langfuse configuration.
    """
    mode = settings.observability.tracing
    if mode == "file":
        return SpoolReader(directory=settings.observability.trace_spool_dir)
    if mode == "otlp":
        base = langfuse_base_url(
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        )
        authorization = langfuse_auth_header()
        if not base or not authorization:
            raise TraceExportUnavailable(
                ("INQTRIX_TRACING=otlp, but "
                "OTEL_EXPORTER_OTLP_ENDPOINT/OTEL_EXPORTER_OTLP_HEADERS are not "
                "configured for Langfuse; export requires the Langfuse REST API.")
            )
        return LangfuseReader(base_url=base, authorization=authorization)
    raise TraceExportUnavailable(
        f'No trace sink is active (INQTRIX_TRACING={mode}): off/local modes record no span details. Set INQTRIX_TRACING=file for later export, or otlp for live traces.'
    )
