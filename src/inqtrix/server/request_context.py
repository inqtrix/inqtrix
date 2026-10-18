"""Outermost ASGI middleware binding the per-request log context.

Every HTTP request gets a request id — an incoming ``X-Request-ID``
(validated, so a client cannot inject log content) or a fresh hex uuid.
The id is bound into :mod:`inqtrix.observability.context` for the
lifetime of the request (so EVERY log line inside carries it, including
error paths — the reason this middleware must wrap all others) and is
echoed as an ``X-Request-ID`` response header so a support report can
quote the exact id to grep for.

The middleware additionally binds the request ORIGIN facts (client ip
resolved with the same trusted-proxy-hops policy as the login throttle,
plus the user agent) into :mod:`inqtrix.auth.request_origin` — read
ONLY by audit writers, never by the log formatter (PII stays out of
operational logs).

It also opens the SERVER span for the request. That span is what makes
the trace chain real: without an active span, ``inject_traceparent``
writes nothing into the persisted run payload, so the worker would open
a disconnected root span and "one trace from request to LLM call" would
be a claim rather than a fact. An incoming ``traceparent`` is extracted
first, so an upstream caller's trace continues through Inqtrix. Probe
and scrape endpoints are excluded — they would otherwise dominate the
trace volume without ever being read.

The span is named ``{method} {http.route}`` from the matched route
TEMPLATE, read once routing has run, and ``{method}`` alone when nothing
matched. The raw path never names it: a path segment can be a bearer
credential (a guest-link token), and the span name becomes the trace
name in the tracing backend. ``url.path`` passes the same credential
sanitizer as the logs, and a recorded exception is reduced the way the logs
reduce one: type, stable code and frame locations, never its text.

Pure ASGI on purpose: no BaseHTTPMiddleware task hop, no body access,
nothing measurable on the hot path.
"""

from __future__ import annotations

import logging
import re
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from starlette.datastructures import Headers, MutableHeaders

from inqtrix.auth.ratelimit import client_ip
from inqtrix.auth.request_origin import (
    bind_request_origin,
    reset_request_origin,
)
from inqtrix.observability.context import bind_log_context, reset_log_context
from inqtrix.urls import sanitize_log_message

log = logging.getLogger("inqtrix")

# Conservative charset/length: anything else is replaced, never echoed.
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# Liveness/readiness/scrape traffic is high-frequency and carries no
# diagnostic value in a trace waterfall.
_UNTRACED_PATHS = frozenset({"/health", "/readyz", "/metrics"})

# Service-to-service coordination (collaboration policy polling, lease
# renewal, compaction). It is continuous background chatter — measured
# live, ONE knowledge run's window carried 40 such spans against 21 for
# the run itself, which would bury every user-initiated trace.
_UNTRACED_PREFIXES = ("/internal/",)


class RequestContextMiddleware:
    """Bind ``request_id`` + origin for the request; echo the id back."""

    def __init__(
        self, app: Callable[..., Any], *, trusted_proxy_hops: int = 1
    ) -> None:
        self.app = app
        self._trusted_proxy_hops = trusted_proxy_hops

    async def __call__(
        self, scope: dict, receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        incoming = headers.get("x-request-id", "")
        request_id = (
            incoming
            if _REQUEST_ID_PATTERN.fullmatch(incoming)
            else uuid.uuid4().hex
        )
        tokens = bind_log_context(request_id=request_id)
        # client_ip expects a Request-shaped object; a namespace over the
        # raw scope avoids constructing a full Request on the hot path
        # while reusing the ONE XFF-from-the-right implementation.
        client = scope.get("client")
        peer = (
            SimpleNamespace(host=client[0])
            if isinstance(client, (tuple, list)) and client
            else None
        )
        origin_tokens = bind_request_origin(
            ip=client_ip(
                SimpleNamespace(client=peer, headers=headers),
                self._trusted_proxy_hops,
            ),
            user_agent=headers.get("user-agent", ""),
        )

        try:
            with _server_span(scope, headers, request_id) as record_status:

                async def send_with_request_id(message: dict) -> None:
                    if message["type"] == "http.response.start":
                        MutableHeaders(scope=message).append(
                            "X-Request-ID", request_id
                        )
                        record_status(int(message.get("status", 0) or 0))
                    await send(message)

                await self.app(scope, receive, send_with_request_id)
        finally:
            # The server reuses tasks/threads across requests in some
            # execution paths — never let request context leak forward.
            reset_request_origin(origin_tokens)
            reset_log_context(tokens)


def _ignore_status(status_code: int) -> None:
    """Status sink for the untraced and no-extra paths."""


_route_naming_warned = False


@contextmanager
def _server_span(
    scope: dict, headers: Headers, request_id: str
) -> Iterator[Callable[[int], None]]:
    """Open the request's server span, continuing an upstream trace.

    Yields a callable that records the response status on the span. Without
    it a trace showed only method and path, so a 4xx read exactly like a 2xx
    and every status question had to be answered from the access log instead.

    Per the HTTP semantic conventions only 5xx marks a SERVER span as an
    error: a 4xx is the client's fault, not the server's, so it stays UNSET
    and is found through the attribute.

    Degrades to a plain no-op without the observability extra or with
    tracing off — the request path must never depend on telemetry.
    """
    path = scope.get("path", "")
    if path in _UNTRACED_PATHS or path.startswith(_UNTRACED_PREFIXES):
        yield _ignore_status
        return
    try:
        from opentelemetry import trace as otel_trace

        from inqtrix.observability import semconv
        from inqtrix.observability.otel import projected_exceptions
        from inqtrix.observability.propagation import (
            extract_incoming_context,
        )
    except Exception:  # noqa: BLE001 — extra not installed
        yield _ignore_status
        return
    parent = extract_incoming_context(headers)
    method = str(scope.get("method", "") or "")
    tracer = otel_trace.get_tracer("inqtrix.server")
    # The SDK's own exception recording is switched off: it would write the
    # exception text, which can repeat the path and spell out document text
    # or personal data. projected_exceptions below records the same event the
    # way the logs record an exception instead.
    with tracer.start_as_current_span(
        method or "HTTP",
        context=parent,
        kind=otel_trace.SpanKind.SERVER,
        attributes={
            semconv.HTTP_REQUEST_METHOD: method,
            semconv.URL_PATH: sanitize_log_message(path),
            semconv.INQTRIX_REQUEST_ID: request_id,
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:

        def name_by_route() -> None:
            # Routing writes the matched route into the shared scope; it is
            # there once the response starts and when the app returns.
            global _route_naming_warned
            try:
                template = getattr(scope.get("route"), "path", None)
                if isinstance(template, str) and template:
                    span.update_name(f"{method} {template}".strip())
                    span.set_attribute(semconv.HTTP_ROUTE, template)
            except Exception:  # noqa: BLE001 — telemetry never breaks the request
                if not _route_naming_warned:
                    _route_naming_warned = True
                    log.warning(
                        "The request span could not be named after its route; "
                        "affected spans keep the method as their name.",
                        exc_info=True,
                    )

        def record_status(status_code: int) -> None:
            name_by_route()
            if status_code <= 0:
                return
            span.set_attribute(
                semconv.HTTP_RESPONSE_STATUS_CODE, status_code
            )
            if status_code >= 500:
                span.set_status(
                    otel_trace.Status(otel_trace.StatusCode.ERROR)
                )

        try:
            with projected_exceptions(span):
                yield record_status
        finally:
            name_by_route()
