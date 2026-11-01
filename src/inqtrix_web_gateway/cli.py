"""Process boundary and uvicorn lifecycle for the Python gateway."""

from __future__ import annotations

import argparse
import logging
import os
import ssl
from collections.abc import Sequence
from typing import Any, TypedDict

import uvicorn
from uvicorn.config import SSL_PROTOCOL_VERSION, create_ssl_context

from . import app as gateway_app
from . import settings
from .logging import configure_logging, gateway_log_config

log = logging.getLogger("inqtrix.web_gateway")

# The API server lists this variable as retired; the gateway cannot import
# that list, so it names the same variable here and a test keeps both equal.
_RETIRED_PROXY_ALLOW_LIST = "FORWARDED_ALLOW_IPS"

# When the process stops, open requests get this long to finish. The
# browser's event streams never end on their own and are cancelled then, so
# the gateway exits before the 10-second stop timeout of Docker and Podman.
_GRACEFUL_SHUTDOWN_SECONDS = 8


class _ServerOptions(TypedDict, total=False):
    host: str
    port: int
    timeout_keep_alive: int
    timeout_graceful_shutdown: int
    ws_max_size: int
    ws_max_queue: int
    workers: int
    access_log: bool
    proxy_headers: bool
    log_config: dict[str, Any]
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
    if _RETIRED_PROXY_ALLOW_LIST in os.environ:
        # The same retired setting the API server reports: the gateway reads
        # forwarding headers itself, so uvicorn's allow list has no effect.
        log.warning(
            "Retired setting %s is set and has no effect; the gateway reads "
            "X-Forwarded-For itself.",
            _RETIRED_PROXY_ALLOW_LIST,
        )
    ssl_options = settings._resolve_ssl_options()
    collaboration_settings = settings.CollaborationProxySettings.from_env()
    server_options = _ServerOptions(
        host=settings._resolve_bind_host(),
        port=settings._resolve_bind_port(),
        timeout_keep_alive=300,
        timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        ws_max_size=collaboration_settings.max_frame_bytes,
        ws_max_queue=collaboration_settings.max_queued_frames,
        workers=workers,
        access_log=False,
        # The gateway reads forwarding headers itself; uvicorn must not
        # replace the socket peer or the scheme from them.
        proxy_headers=False,
        # uvicorn applies the gateway's configuration in this process and in
        # every worker process it spawns; without ``log_level`` it keeps the
        # levels of the mapping.
        log_config=gateway_log_config(),
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
