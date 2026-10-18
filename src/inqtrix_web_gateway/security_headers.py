"""Baseline security headers for every response the edge serves.

The guest surface already carried a subset (``static.py``,
``deploy/nginx/static-headers.conf``); these apply to the AUTHENTICATED shell
and the proxied API as well, because that is where the session cookie lives.

Deliberately not here: a Content-Security-Policy. A policy that misses one
source silently breaks a feature — fonts, the PDF worker, Mermaid, the
collaboration WebSocket — and must be built report-only against the real
application first. That is its own piece of work, not a line in this file.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

FRAME_POLICY = b"DENY"
"""Inqtrix is never embedded in a third-party page.

An authenticated tool that can be framed can be clickjacked: the attacker
overlays their own page, the victim believes they are clicking there, and the
click lands in the framed session instead.
"""

REFERRER_POLICY = b"strict-origin-when-cross-origin"
"""Never leak a path to a third party, keep same-origin navigation useful.

The guest surface keeps its stricter ``no-referrer``; this is the floor for
everything else.
"""

CONTENT_TYPE_OPTIONS = b"nosniff"
"""Serve every response as the type declared, never as one guessed from bytes."""

HSTS_MAX_AGE_SECONDS = 15_552_000
"""180 days. Deliberately not a year, and deliberately without ``preload``.

The promise cannot be withdrawn inside its own window: once a browser has seen
it, that host is HTTPS-only until the age expires. A first rollout should be
able to back out within a season, and a ``preload`` entry is baked into browser
builds and takes months to remove.

``includeSubDomains`` is deliberately absent. It would bind every sibling host
of this domain, which this application neither knows nor can vouch for — a
plain-HTTP tool one label over would become unreachable for the whole window.
Domain-wide policy belongs to whoever owns the domain edge (an ingress
annotation), not to one application behind it.
"""

_HSTS_VALUE = f"max-age={HSTS_MAX_AGE_SECONDS}".encode()


class SecurityHeadersMiddleware:
    """Stamp the baseline headers on every HTTP response.

    Existing values win: a route that already set a stricter policy — the
    guest surface sets ``no-referrer`` — keeps it.
    """

    def __init__(self, app: ASGIApp, *, external_scheme: str | None = None) -> None:
        self.app = app
        # HSTS only means anything over TLS, and announcing it from a plain
        # HTTP deployment would be a promise the deployment cannot keep.
        self._send_hsts = external_scheme == "https"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}
                for name, value in (
                    (b"x-frame-options", FRAME_POLICY),
                    (b"x-content-type-options", CONTENT_TYPE_OPTIONS),
                    (b"referrer-policy", REFERRER_POLICY),
                ):
                    if name not in present:
                        headers.append((name, value))
                if self._send_hsts and b"strict-transport-security" not in present:
                    headers.append((b"strict-transport-security", _HSTS_VALUE))
            await send(message)

        await self.app(scope, receive, send_with_headers)
