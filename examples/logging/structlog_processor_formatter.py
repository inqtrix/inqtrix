"""structlog: ProcessorFormatter renders Inqtrix records, InqtrixLogFilter on its handler.

examples/logging/structlog_processor_formatter.py is an application that logs
with structlog and embeds Inqtrix. Inqtrix logs through the standard
``logging`` module, so its records reach a standard handler. That handler
renders them with ``structlog.stdlib.ProcessorFormatter`` into the same JSON
lines as the application's own structlog events, and it carries
``InqtrixLogFilter()``, so every record is projected and scrubbed before the
formatter sees it. The processor ``add_inqtrix_fields`` copies the correlation
fields the filter stamps (``request_id``, ``run_id``, ``user``, ``tenant`` as
a pseudonym, ``trace_id``, ``span_id``) and the projected exception into the
event.

structlog writes the application's own events itself (``PrintLoggerFactory``)
instead of handing them to that handler: the filter replaces the message of
every record it sees with the rendered text, and an event that structlog
hands to the standard library through ``ProcessorFormatter.wrap_for_formatter``
carries its event dictionary in exactly that message.

structlog is not a dependency of Inqtrix; install it into your application's
environment. A research run needs provider credentials, so
``inqtrix_at_work()`` stands in for one: it binds the correlation fields the
way Inqtrix does at a request or run boundary and logs on ``inqtrix.example``
the way an Inqtrix module logs on its own logger.

Run with:
    python examples/logging/structlog_processor_formatter.py
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from inqtrix import InqtrixLogFilter
from inqtrix.observability import bind_log_context, reset_log_context

# What the filter stamps onto every record. A field that is not bound is None.
CORRELATION_FIELDS = ("request_id", "run_id", "user", "tenant", "trace_id", "span_id")

SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]


def add_inqtrix_fields(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Copy the correlation fields and the projected exception from the record."""

    record = event_dict.get("_record")
    if record is not None:
        for name in CORRELATION_FIELDS:
            value = getattr(record, name, None)
            if value:
                event_dict[name] = value
        if record.exc_text:
            # For an Inqtrix record: types, codes and frame locations only.
            event_dict["exception"] = record.exc_text
    return event_dict


def configure_application_logging() -> None:
    """structlog for the application's events, a filtered handler for standard records."""

    # The application's own events: structlog renders and writes them itself.
    structlog.configure(
        processors=[*SHARED_PROCESSORS, structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    # The records of the standard library: Inqtrix, uvicorn, other libraries.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                *SHARED_PROCESSORS,
                structlog.stdlib.add_logger_name,
                add_inqtrix_fields,
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    handler.addFilter(InqtrixLogFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("inqtrix").setLevel(logging.INFO)


def inqtrix_at_work() -> None:
    """Stand in for a research run: bind the correlation fields, log as Inqtrix does."""

    tokens = bind_log_context(
        request_id="4bf92f3577b34da6a3ce929d0e0e4736", run_id="run-1", tenant="acme-tenant-0042"
    )
    try:
        logging.getLogger("inqtrix.example").info(
            "Run %s segment started (mode=%s).", "run-1", "research"
        )
    finally:
        reset_log_context(tokens)


def main() -> None:
    configure_application_logging()
    structlog.get_logger().info("application_started", component="billing")
    inqtrix_at_work()


if __name__ == "__main__":
    main()
