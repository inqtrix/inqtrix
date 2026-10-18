"""SPA serving policy: fallback, MIME types, and cache/privacy headers."""

from __future__ import annotations

import mimetypes

from fastapi import Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.types import Scope

_ASSETS_PREFIX = "assets/"
_ASSETS_CACHE_CONTROL = "public, max-age=31536000, immutable"
_DEFAULT_CACHE_CONTROL = "no-cache"

# Precompressed siblings, strongest first. The build writes them; the
# gateway never compresses per request, because squeezing a multi-MB
# bundle on every cache miss costs more than the smaller payload saves.
_ENCODINGS = (("br", ".br"), ("gzip", ".gz"))

# Host MIME databases vary and Python added .mjs only recently.
mimetypes.add_type("text/javascript", ".mjs")


def _accepted_encodings(scope: Scope) -> frozenset[str]:
    """Encoding tokens the client accepts, ignoring q-values.

    A q-value of 0 means "refused"; treating it as acceptance would ship
    an encoding the client told us it cannot read.
    """
    raw = ""
    for key, value in scope.get("headers", ()):
        if key == b"accept-encoding":
            raw = value.decode("latin-1")
            break
    accepted: set[str] = set()
    for part in raw.split(","):
        token, _, parameters = part.strip().partition(";")
        if not token:
            continue
        if "q=0" in parameters.replace(" ", "") and "q=0." not in parameters:
            continue
        accepted.add(token.lower())
    return frozenset(accepted)


class SpaStaticFiles(StaticFiles):
    """Static app with hard asset 404s and client-route SPA fallback."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        is_asset = path.startswith(_ASSETS_PREFIX)
        try:
            if is_asset:
                response = await self._asset_response(path, scope)
            else:
                response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or is_asset:
                raise
            # Deliberately the plain loader: a fallback is index.html
            # under a foreign name, so a same-named precompressed
            # sibling must never be negotiated onto it.
            response = await super().get_response("index.html", scope)
        response.headers["cache-control"] = (
            _ASSETS_CACHE_CONTROL if is_asset else _DEFAULT_CACHE_CONTROL
        )
        if path == "s" or path.startswith("s/"):
            response.headers["cache-control"] = "no-store"
            response.headers["referrer-policy"] = "no-referrer"
            response.headers["x-content-type-options"] = "nosniff"
        return response

    async def _asset_response(self, path: str, scope: Scope) -> Response:
        """Serve a hashed asset, preferring a precompressed sibling.

        Only files under the immutable ``/assets/`` prefix take this
        path. They are build output: no secret, no reflected user input,
        so the BREACH/CRIME class that rules out blanket compression of
        authenticated responses does not apply. Dynamic API traffic and
        the SSE streams are proxied elsewhere and stay untouched.
        """
        accepted = _accepted_encodings(scope)
        for encoding, suffix in _ENCODINGS:
            if encoding not in accepted:
                continue
            try:
                response = await super().get_response(f"{path}{suffix}", scope)
            except HTTPException:
                continue
            if response.status_code != 200:
                continue
            response.headers["content-encoding"] = encoding
            # The identity type decides how the browser executes the
            # body; the sibling's own extension would say octet-stream
            # and a module served that way is rejected outright.
            media_type, _ = mimetypes.guess_type(path)
            if media_type:
                response.headers["content-type"] = media_type
            response.headers["vary"] = "accept-encoding"
            # The compressed sibling has its own length and digest; a
            # stale validator would let a cache pair one encoding's
            # body with another's ETag.
            if "etag" in response.headers:
                del response.headers["etag"]
            return response
        return await super().get_response(path, scope)
