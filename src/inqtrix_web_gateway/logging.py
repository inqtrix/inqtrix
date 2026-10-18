"""Credential-safe logging for the web gateway."""

from __future__ import annotations

import logging as stdlib_logging
import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_GUEST_ROUTE_TOKEN_RE = re.compile(
    r"(?i)(/s/|/v1/editor/share-links/)[^/?#\s\"'<>]+"
)
_PERCENT_ENCODED_BYTE_RE = re.compile(r"%([0-9a-f]{2})", re.IGNORECASE)


def _normalize_route_match_characters(value: str) -> str:
    """Decode only URI characters needed to classify a sensitive route.

    httpx retains percent-encoded reserved separators such as ``%2F`` in its
    request log. Decoding the full string would allow encoded controls to
    inject log lines, so this deliberately normalizes only ASCII unreserved
    characters plus slash.
    """

    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        if character.isascii() and (
            character.isalnum() or character in "-._~/"
        ):
            return character
        return match.group(0)

    return _PERCENT_ENCODED_BYTE_RE.sub(replace, value)


def _redact_guest_route_tokens(value: str) -> str:
    """Redact account-less bearer tokens while retaining route diagnostics."""
    value = _normalize_route_match_characters(value)
    return _GUEST_ROUTE_TOKEN_RE.sub(
        lambda match: f"{match.group(1)}[REDACTED]",
        value,
    )


class _GuestTokenRedactionFilter(stdlib_logging.Filter):
    """Scrub guest bearer paths from application and dependency records."""

    def filter(self, record: stdlib_logging.LogRecord) -> bool:
        record.msg = _redact_guest_route_tokens(record.getMessage())
        record.args = ()
        return True


def configure_logging() -> None:
    """Configure one sanitized console pipeline for uvicorn and the gateway."""
    stdlib_logging.basicConfig(
        level=stdlib_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    root = stdlib_logging.getLogger()
    for handler in root.handlers:
        if not any(
            isinstance(filter_, _GuestTokenRedactionFilter)
            for filter_ in handler.filters
        ):
            handler.addFilter(_GuestTokenRedactionFilter())


class AccessLogMiddleware:
    """Emit sanitized HTTP access lines with method, status, and duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status_code = 500

        async def send_with_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            stdlib_logging.getLogger("inqtrix.web_gateway.access").info(
                "%s %s %d %.1fms",
                str(scope.get("method") or "-"),
                _redact_guest_route_tokens(str(scope.get("path") or "/")),
                status_code,
                (time.perf_counter() - started) * 1000,
            )
