"""Process boundary and uvicorn lifecycle for the Python gateway."""

from __future__ import annotations

import argparse
import logging
import os
import ssl
from collections.abc import Sequence
from typing import TypedDict

import uvicorn
from uvicorn.config import SSL_PROTOCOL_VERSION, create_ssl_context

from . import app as gateway_app
from . import settings
from .logging import configure_logging

log = logging.getLogger("inqtrix.web_gateway")


class _ServerOptions(TypedDict, total=False):
    host: str
    port: int
    log_level: str
    timeout_keep_alive: int
    ws_max_size: int
    ws_max_queue: int
    workers: int
    access_log: bool
    log_config: None
    ssl_certfile: str
    ssl_keyfile: str
    ssl_keyfile_password: str


def _argument_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""
    parser = argparse.ArgumentParser(
        prog="python -m inqtrix_web_gateway",
        description="Serve the Inqtrix web application and proxy its backend.",
    )
    parser.add_argument(
        "--dist-dir",
        help="React build directory (overrides INQTRIX_DIST_DIR).",
    )
    parser.add_argument(
        "--backend-url",
        help="Backend base URL (overrides INQTRIX_BACKEND_URL).",
    )
    parser.add_argument(
        "--host",
        help="Bind host (overrides RESEARCH_DESK_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Bind port (overrides RESEARCH_DESK_PORT).",
    )
    return parser


def _apply_cli_overrides(arguments: argparse.Namespace) -> None:
    """Publish explicit CLI values for this process and spawned workers."""
    values = {
        "INQTRIX_DIST_DIR": arguments.dist_dir,
        "INQTRIX_BACKEND_URL": arguments.backend_url,
        "RESEARCH_DESK_HOST": arguments.host,
        "RESEARCH_DESK_PORT": arguments.port,
    }
    for name, value in values.items():
        if value is not None:
            os.environ[name] = str(value)


def main(argv: Sequence[str] | None = None) -> None:
    """Validate configuration in the parent and run uvicorn."""
    arguments = _argument_parser().parse_args(argv)
    _apply_cli_overrides(arguments)
    configure_logging()
    workers = settings._resolve_worker_count()
    web_concurrency = os.getenv("WEB_CONCURRENCY", "").strip()
    if web_concurrency and web_concurrency != str(workers):
        log.warning(
            "WEB_CONCURRENCY=%s is ignored; the gateway reads only "
            "RESEARCH_DESK_WORKERS (currently %d).",
            web_concurrency,
            workers,
        )
    ssl_options = settings._resolve_ssl_options()
    collaboration_settings = settings.CollaborationProxySettings.from_env()
    server_options = _ServerOptions(
        host=settings._resolve_bind_host(),
        port=settings._resolve_bind_port(),
        log_level="info",
        timeout_keep_alive=300,
        ws_max_size=collaboration_settings.max_frame_bytes,
        ws_max_queue=collaboration_settings.max_queued_frames,
        workers=workers,
        access_log=False,
        log_config=None,
    )
    if certfile := ssl_options.get("ssl_certfile"):
        server_options["ssl_certfile"] = certfile
    if keyfile := ssl_options.get("ssl_keyfile"):
        server_options["ssl_keyfile"] = keyfile
    if password := ssl_options.get("ssl_keyfile_password"):
        server_options["ssl_keyfile_password"] = password
    if workers == 1:
        uvicorn.run(gateway_app.create_app_from_env(), **server_options)
        return

    # Uvicorn respawns failed children indefinitely. Validate all static app
    # configuration and TLS material in the parent before starting its
    # multi-process supervisor.
    gateway_app.create_app_from_env()
    if ssl_options:
        create_ssl_context(
            certfile=str(ssl_options["ssl_certfile"]),
            keyfile=str(ssl_options["ssl_keyfile"]),
            password=str(ssl_options.get("ssl_keyfile_password") or "") or None,
            ssl_version=SSL_PROTOCOL_VERSION,
            cert_reqs=ssl.CERT_NONE,
            ca_certs=None,
            ciphers="TLSv1",
        )
    uvicorn.run(
        "inqtrix_web_gateway.app:create_app_from_env",
        factory=True,
        **server_options,
    )
