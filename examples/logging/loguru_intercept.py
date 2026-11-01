"""loguru: standard logging routed into loguru, InqtrixLogFilter on the intercept handler.

examples/logging/loguru_intercept.py is an application that logs with loguru
and embeds Inqtrix. Inqtrix logs through the standard ``logging`` module; the
intercept handler from loguru's documentation hands each of those records to
loguru. ``InqtrixLogFilter()`` sits on that intercept handler, so a record is
projected and scrubbed before loguru sees it. The handler binds the
correlation fields the filter stamps (``request_id``, ``run_id``, ``user``,
``tenant`` as a pseudonym, ``trace_id``, ``span_id``) and the projected
exception as loguru extra fields. loguru's own messages never pass through the
standard library and are not touched.

The filter has replaced ``exc_info`` by ``exc_text`` on every record it
projected, so the handler passes ``exc_text`` on instead of handing loguru an
exception.

loguru is not a dependency of Inqtrix; install it into your application's
environment. A research run needs provider credentials, so
``inqtrix_at_work()`` stands in for one: it binds the correlation fields the
way Inqtrix does at a request or run boundary and logs on ``inqtrix.example``
the way an Inqtrix module logs on its own logger.

Run with:
    python examples/logging/loguru_intercept.py
"""

from __future__ import annotations

import inspect
import logging
import sys

from loguru import logger

from inqtrix import InqtrixLogFilter
from inqtrix.observability import bind_log_context, reset_log_context

# What the filter stamps onto every record. A field that is not bound is None.
CORRELATION_FIELDS = ("request_id", "run_id", "user", "tenant", "trace_id", "span_id")


class InterceptHandler(logging.Handler):
    """Hand every record of the standard library to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Find the caller that logged the record, outside the logging module.
        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1
        fields = {
            name: getattr(record, name)
            for name in CORRELATION_FIELDS
            if getattr(record, name, None)
        }
        if record.exc_text:
            # For an Inqtrix record: types, codes and frame locations only.
            fields["exception"] = record.exc_text
        logger.bind(**fields).opt(depth=depth).log(level, record.getMessage())


def configure_application_logging() -> None:
    """loguru writes JSON lines; standard records reach it through the filtered handler."""

    logger.remove()
    logger.add(sys.stdout, serialize=True, level="INFO")
    handler = InterceptHandler()
    handler.addFilter(InqtrixLogFilter())
    logging.basicConfig(handlers=[handler], level=logging.WARNING, force=True)
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
    logger.info("Application started.")
    inqtrix_at_work()


if __name__ == "__main__":
    main()
