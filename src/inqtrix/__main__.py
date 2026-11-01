"""Entry point: ``python -m inqtrix``.

Starts the FastAPI server on port 5100 (override via
``INQTRIX_SERVER_PORT``). Logging is configured first, from the
``INQTRIX_LOG_*`` variables (see :func:`inqtrix.logging_config.configure_logging`):
by default INFO as JSON on stdout. Uvicorn runs without its own logging
configuration, so its startup and access lines go through the same root
handlers, filter and format as every application record.
"""

from __future__ import annotations

import os
from typing import Any

import uvicorn

from inqtrix.logging_config import configure_logging, log_status_line
from inqtrix.server import create_app
from inqtrix.settings import Settings, check_retired_settings


def _server_port() -> int:
    """``INQTRIX_SERVER_PORT`` as a number, or a start error that names it."""
    try:
        return int(os.getenv("INQTRIX_SERVER_PORT", "5100"))
    except ValueError:
        # ``int()`` quotes the value; the name is what the operator needs.
        raise RuntimeError("INQTRIX_SERVER_PORT must be an integer.") from None


def main() -> None:
    """Configure logging, then build the app and start uvicorn."""
    resolved = configure_logging(service="api")
    retired = check_retired_settings()
    log_status_line(resolved, retired=retired)
    settings = Settings()
    app = create_app(settings=settings)
    uvicorn_kwargs: dict[str, Any] = dict(
        host=os.getenv("INQTRIX_SERVER_HOST", "0.0.0.0"),
        port=_server_port(),
        workers=1,
        timeout_keep_alive=300,
        ws_max_size=settings.collaboration.max_frame_bytes,
        ws_max_queue=settings.collaboration.max_queued_frames,
        log_config=None,
        # Inqtrix reads X-Forwarded-For itself (INQTRIX_TRUSTED_PROXY_HOPS);
        # uvicorn must not replace the socket peer or the scheme from it.
        proxy_headers=False,
    )
    uvicorn.run(app, **uvicorn_kwargs)


if __name__ == "__main__":
    main()
