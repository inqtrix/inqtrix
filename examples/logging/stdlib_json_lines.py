"""Standard logging: your own handler and JSON formatter with InqtrixLogFilter.

examples/logging/stdlib_json_lines.py is an application that embeds Inqtrix
and owns its logging. It builds its own handler and formatter (JSON lines on
stdout) and adds ``InqtrixLogFilter()`` to that handler. It does not call
``configure_logging(...)``, which would configure the root logger of the whole
process, and it does not put the filter on the ``inqtrix`` logger, whose
filters never see the records of the module loggers below it.

On every record that reaches the handler, the filter stamps the correlation
fields as record attributes (``request_id``, ``run_id``, ``user``, ``tenant``
as a pseudonym, ``trace_id``, ``span_id``; ``None`` when not bound), shows an
exception of an Inqtrix record only as its type, a stable code and frame
locations, and scrubs known secret shapes from every message, the
application's own included. The application's own records otherwise keep
their levels and messages.

A research run needs provider credentials, so ``inqtrix_at_work()`` stands in
for one: it binds the correlation fields the way Inqtrix does at a request or
run boundary and logs on ``inqtrix.example`` the way an Inqtrix module logs
on its own logger (for example ``inqtrix.providers.bedrock``). The script
needs no network.

Run with:
    uv run python examples/logging/stdlib_json_lines.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from inqtrix import InqtrixLogFilter
from inqtrix.observability import bind_log_context, reset_log_context

# What the filter stamps onto every record. A field that is not bound is None.
CORRELATION_FIELDS = ("request_id", "run_id", "user", "tenant", "trace_id", "span_id")

REQUEST_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
RUN_ID = "3f6b2c1e-9a4d-4e8b-b1c2-5d7e9f0a1b2c"
TENANT_ID = "acme-tenant-0042"
QUESTION = "What does contract ACME-4711 say about termination?"


class JsonLinesFormatter(logging.Formatter):
    """One JSON object per record, with the field names of the application."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in CORRELATION_FIELDS:
            value = getattr(record, name, None)
            if value:
                entry[name] = value
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            # For an Inqtrix record the filter has already reduced the
            # exception to types, codes and frame locations.
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


def configure_application_logging() -> None:
    """The application's logging: one handler, one formatter, the filter on the handler."""

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLinesFormatter())
    handler.addFilter(InqtrixLogFilter())
    logging.basicConfig(level=logging.WARNING, handlers=[handler])
    logging.getLogger("inqtrix").setLevel(logging.INFO)


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
    configure_application_logging()
    inqtrix_at_work()
    # The application's own record keeps its message; the password in the
    # connection URL is scrubbed.
    logging.getLogger("acme.storage").warning(
        "Database connection to %s failed; retrying.",
        "postgresql://acme:s3cret-Passw0rd@db.internal:5432/acme",
    )


if __name__ == "__main__":
    main()
