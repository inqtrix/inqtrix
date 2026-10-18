"""Shared span-recording helpers for tests.

The OpenTelemetry SDK is imported only when a helper runs, so importing this
module never breaks collection in an environment without the observability
extra.
"""

from __future__ import annotations

import json
from typing import Any


class ProxyTrace:
    """Stand-in for ``inqtrix.observability.otel._otel_trace``.

    Routes ``get_tracer`` to a test-local provider, so every span opened
    through ``inqtrix.observability.otel`` is recorded there and nothing is
    installed process-globally.
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def get_tracer(self, name: str, tracer_provider: Any = None) -> Any:
        return (tracer_provider or self._provider).get_tracer(name)

    def get_current_span(self) -> Any:
        from opentelemetry import trace as real_trace

        return real_trace.get_current_span()

    def set_tracer_provider(self, provider: Any) -> None:  # pragma: no cover
        raise AssertionError("tests must never install a global provider")


def in_memory_provider(**provider_kwargs: Any) -> tuple[Any, Any]:
    """A test-local ``TracerProvider`` and the in-memory exporter behind it."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider(**provider_kwargs)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def everything_a_span_exports(span: Any) -> str:
    """Name, attributes, events and status text: every place a span can carry
    text into a trace backend."""
    parts = [span.name, json.dumps(dict(span.attributes or {}), default=str)]
    for event in span.events:
        parts.append(event.name)
        parts.append(json.dumps(dict(event.attributes or {}), default=str))
    parts.append(str(span.status.description or ""))
    return "\n".join(parts)
