"""Opt-in security helpers for the Inqtrix HTTP server.

Three independent layers, all off-by-default and gated by env-driven
``ServerSettings`` fields:

* :func:`resolve_tls_paths` — reads ``tls_keyfile`` + ``tls_certfile``
  and returns the pair the example ``main()`` functions hand to
  ``uvicorn.run(...)``. Partial configuration is rejected loudly.
* :func:`make_api_key_dependency` — when ``api_key`` is set, returns a
  FastAPI dependency that enforces ``Authorization: Bearer <api_key>``
  with constant-time comparison (``hmac.compare_digest``). ``None``
  result means the gate is disabled.
* :func:`make_cors_middleware_kwargs` — when ``cors_origins`` is set,
  returns ``add_middleware(CORSMiddleware, **kwargs)`` keyword
  arguments. ``None`` means no middleware should be installed.

All three helpers are pure functions so they can be unit-tested
without booting a TestClient. The ``ServerSettings`` carrier is the
only env-coupled surface; library callers may construct a
``ServerSettings`` programmatically instead.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import Request

from inqtrix.auth.api_key import build_bearer_guard
from inqtrix.settings import ServerSettings

log = logging.getLogger("inqtrix")


def resolve_tls_paths(server: ServerSettings) -> tuple[str, str] | None:
    """Resolve the TLS keyfile + certfile pair for ``uvicorn.run(...)``.

    Args:
        server: The resolved :class:`ServerSettings`. Only
            ``tls_keyfile`` and ``tls_certfile`` are inspected.

    Returns:
        A ``(keyfile, certfile)`` tuple when both fields are non-empty,
        ``None`` when both are empty.

    Raises:
        RuntimeError: When exactly one of the two paths is provided.
            TLS half-configured is almost always a deployment mistake;
            we fail loudly so it surfaces during the first start
            instead of silently downgrading to HTTP.
    """
    keyfile = (server.tls_keyfile or "").strip()
    certfile = (server.tls_certfile or "").strip()
    if not keyfile and not certfile:
        return None
    if not (keyfile and certfile):
        raise RuntimeError(
            ("INQTRIX_SERVER_TLS_KEYFILE and INQTRIX_SERVER_TLS_CERTFILE must be set "
            "together (both or neither).")
        )
    return keyfile, certfile


def make_api_key_dependency(
    server: ServerSettings,
) -> Callable[[Request], None] | None:
    """Build a FastAPI dependency that enforces a Bearer API-key gate.

    Args:
        server: The resolved :class:`ServerSettings`. Only ``api_key``
            is inspected.

    Returns:
        A FastAPI-compatible dependency function that raises
        :class:`HTTPException` (401) on missing or wrong credentials,
        or ``None`` when the gate is disabled (``api_key`` empty).
        Returning ``None`` is the signal to the route registrar that
        no dependency should be wired in.
    """
    expected = (server.api_key or "").strip()
    if not expected:
        return None
    return build_bearer_guard(expected)


def make_cors_middleware_kwargs(server: ServerSettings) -> dict[str, Any] | None:
    """Build ``CORSMiddleware`` kwargs from the comma-separated origin list.

    Args:
        server: The resolved :class:`ServerSettings`. Only
            ``cors_origins`` is inspected.

    Returns:
        A dict suitable for
        ``app.add_middleware(CORSMiddleware, **kwargs)`` when at least
        one origin is configured, ``None`` when the field is empty.
        Wildcard origins are accepted but emit a WARNING log line
        because browsers refuse wildcard + credentials.
    """
    raw = (server.cors_origins or "").strip()
    if not raw:
        return None
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if not origins:
        return None
    if any(origin == "*" for origin in origins):
        log.warning(
            "CORS wildcard '*' is active; browsers ignore the wildcard when "
            "allow_credentials=True. Do not use this in production."
        )
    return {
        "allow_origins": origins,
        "allow_methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": [
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "X-CSRF-Token",
            "X-Inqtrix-Expected-User-Id",
            "X-Inqtrix-Workspace-Id",
        ],
        "allow_credentials": True,
    }
