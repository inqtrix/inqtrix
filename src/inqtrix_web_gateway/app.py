"""Application composition root for the default Python web edge."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

from . import settings
from .http_proxy import build_backend_client, register_http_routes
from .logging import AccessLogMiddleware, configure_logging
from .security_headers import SecurityHeadersMiddleware
from .static import SpaStaticFiles
from .websocket_proxy import register_websocket_route

log = logging.getLogger("inqtrix.web_gateway")


def build_app(
    dist_dir: Path,
    backend_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    collaboration_settings: settings.CollaborationProxySettings | None = None,
    public_base_url: str | None = None,
    external_scheme: str | None = None,
    max_request_bytes: int | None = None,
    max_upstream_connections: int | None = None,
) -> FastAPI:
    """Compose validated policy, protocol adapters, and the SPA fallback."""
    backend_url = settings._parse_backend_origin(backend_url)
    collaboration_settings = (
        collaboration_settings or settings.CollaborationProxySettings()
    )
    public_origin = settings._parse_public_origin(public_base_url)
    external_scheme = settings._parse_external_scheme(external_scheme)
    if (
        public_origin is not None
        and external_scheme is not None
        and public_origin.scheme != external_scheme
    ):
        raise ValueError(
            "INQTRIX_EXTERNAL_SCHEME must match the scheme in "
            "INQTRIX_PUBLIC_BASE_URL when both are set"
        )
    resolved_max_request_bytes = (
        settings._validate_positive_int(
            max_request_bytes, env_var="INQTRIX_PROXY_MAX_BODY_BYTES"
        )
        or settings._DEFAULT_MAX_REQUEST_BYTES
    )
    resolved_max_connections = (
        settings._validate_positive_int(
            max_upstream_connections,
            env_var="INQTRIX_MAX_UPSTREAM_CONNECTIONS",
        )
        or settings._DEFAULT_MAX_UPSTREAM_CONNECTIONS
    )
    client = build_backend_client(
        backend_url,
        max_connections=resolved_max_connections,
        transport=transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "Serving %s and proxying /v1, /api, /health, /readyz, "
            "/collaboration/instance, /collaboration to %s",
            dist_dir,
            backend_url,
        )
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="Inqtrix web gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware, external_scheme=external_scheme
    )
    register_websocket_route(
        app,
        backend_url=backend_url,
        public_origin=public_origin,
        external_scheme=external_scheme,
        settings=collaboration_settings,
    )
    register_http_routes(
        app,
        client=client,
        public_origin=public_origin,
        external_scheme=external_scheme,
        max_request_bytes=resolved_max_request_bytes,
        max_upstream_connections=resolved_max_connections,
    )
    app.mount(
        "/",
        SpaStaticFiles(directory=str(dist_dir), html=False),
        name="frontend",
    )
    return app


def create_app_from_env() -> FastAPI:
    """Resolve the environment once and build the uvicorn factory app."""
    configure_logging()
    settings._validate_web_adapter()
    return build_app(
        settings._resolve_dist_dir(),
        settings._resolve_backend_url(),
        collaboration_settings=settings.CollaborationProxySettings.from_env(),
        public_base_url=os.getenv("INQTRIX_PUBLIC_BASE_URL", "").strip() or None,
        external_scheme=os.getenv("INQTRIX_EXTERNAL_SCHEME", "").strip() or None,
        max_request_bytes=settings._resolve_proxy_max_body_bytes(),
        max_upstream_connections=settings._parse_optional_int(
            os.getenv("INQTRIX_MAX_UPSTREAM_CONNECTIONS"),
            env_var="INQTRIX_MAX_UPSTREAM_CONNECTIONS",
        ),
    )
