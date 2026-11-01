"""OpenTelemetry logs: InqtrixLogFilter on the handler that exports your log records.

examples/logging/opentelemetry_logs.py is an application that embeds Inqtrix
and sends its log records into an OpenTelemetry logs pipeline. The SDK's
``LoggingHandler`` turns each standard ``logging`` record into an
OpenTelemetry log record; ``InqtrixLogFilter()`` sits on that handler, so a
record is projected and scrubbed before the pipeline sees it.

The handler exports every record attribute that is not a standard
``LogRecord`` field as a log attribute, so the correlation fields the filter
stamps (``request_id``, ``run_id``, ``user``, ``tenant`` as a pseudonym,
``trace_id``, ``span_id``) arrive at the collector as attributes, and the
handler also takes the trace and span of the active span. It builds its
``exception.*`` attributes only from ``exc_info``, which the filter has
replaced by the projected exception in ``exc_text`` (types, codes and frame
locations). A second, small filter therefore copies that projection into the
attribute ``exception.stacktrace``; without it, the exported record keeps
nothing of a logged exception but what its message says.

The script exports to the console instead of a collector and needs no
network. To send the records to a collector, replace
``ConsoleLogRecordExporter`` with ``OTLPLogExporter`` from
``opentelemetry.exporter.otlp.proto.http._log_exporter`` and
``SimpleLogRecordProcessor`` with ``BatchLogRecordProcessor``. The SDK and the
exporter belong to the ``observability`` extra of Inqtrix. A research run
needs provider credentials, so ``inqtrix_at_work()`` stands in for one: it
binds the correlation fields the way Inqtrix does at a request or run boundary
and logs on ``inqtrix.example`` the way an Inqtrix module logs on its own
logger.

Run with:
    uv run python examples/logging/opentelemetry_logs.py
"""

from __future__ import annotations

import logging
import sys

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    ConsoleLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

from inqtrix import InqtrixLogFilter
from inqtrix.observability import bind_log_context, reset_log_context

REQUEST_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
RUN_ID = "3f6b2c1e-9a4d-4e8b-b1c2-5d7e9f0a1b2c"
TENANT_ID = "acme-tenant-0042"
QUESTION = "What does contract ACME-4711 say about termination?"


class ExportProjectedException(logging.Filter):
    """Copy the projected exception into an attribute the handler exports.

    Added after ``InqtrixLogFilter``: the projection exists only once that
    filter has run.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_text:
            setattr(record, "exception.stacktrace", record.exc_text)
        return True


def configure_application_logging() -> tuple[LoggerProvider, LoggingHandler]:
    """One logs pipeline, one handler, the filters on the handler."""

    provider = LoggerProvider(resource=Resource.create({"service.name": "acme-app"}))
    provider.add_log_record_processor(
        SimpleLogRecordProcessor(
            ConsoleLogRecordExporter(
                out=sys.stdout,
                formatter=lambda log_record: log_record.to_json(indent=None) + "\n",
            )
        )
    )
    handler = LoggingHandler(logger_provider=provider)
    handler.addFilter(InqtrixLogFilter())
    handler.addFilter(ExportProjectedException())
    # Not logging.basicConfig: it would give the handler a formatter, and the
    # handler would then export the formatted text as the body.
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("inqtrix").setLevel(logging.INFO)
    return provider, handler


class ProviderError(RuntimeError):
    """Like a provider SDK error: its message quotes the request it failed on."""

    status_code = 429


def inqtrix_at_work() -> None:
    """Stand in for a research run: bind the correlation fields, log as Inqtrix does."""

    tokens = bind_log_context(request_id=REQUEST_ID, run_id=RUN_ID, tenant=TENANT_ID)
    try:
        log = logging.getLogger("inqtrix.example")
        log.info("Run %s segment started (mode=%s).", RUN_ID, "research")
        try:
            raise ProviderError(f"Rate limit reached for the request {QUESTION!r}")
        except ProviderError as exc:
            log.warning("Provider call failed: %s", exc, exc_info=True)
    finally:
        reset_log_context(tokens)


def main() -> None:
    provider, handler = configure_application_logging()
    tracer = TracerProvider().get_tracer("acme.app")
    try:
        # The active span of the request: the filter stamps its ids as
        # attributes, the handler sets them as the record's trace context.
        with tracer.start_as_current_span("handle request"):
            inqtrix_at_work()
        logging.getLogger("acme.storage").warning(
            "Database connection to %s failed; retrying.",
            "postgresql://acme:s3cret-Passw0rd@db.internal:5432/acme",
        )
    finally:
        logging.getLogger().removeHandler(handler)
        provider.shutdown()


if __name__ == "__main__":
    main()
