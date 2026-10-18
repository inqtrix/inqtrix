"""Cross-cutting observability building blocks.

One home for the telemetry fundament: request/run log-context
(:mod:`~inqtrix.observability.context`) and the structured JSON log
formatter (:mod:`~inqtrix.observability.json_formatter`). Tracing,
content policy, and metrics definitions join this package in later
build-out steps so observability concerns never scatter again.

Layering rule: this package sits BELOW the server/worker layers — it
must not import settings, providers, or FastAPI. Consumers push values
in (``bind_log_context``); nothing here pulls configuration.
"""

from inqtrix.observability.context import (
    bind_log_context,
    bind_principal_context,
    current_log_context,
    reset_log_context,
)

__all__ = [
    "bind_log_context",
    "bind_principal_context",
    "current_log_context",
    "reset_log_context",
]
