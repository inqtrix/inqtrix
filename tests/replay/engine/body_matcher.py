"""Compare request bodies so that key order and whitespace do not count."""

from __future__ import annotations

import json
from typing import Any


def normalized_body(request: Any) -> Any:
    """Return the parsed JSON body, or the plain body when it is not JSON."""
    body = request.body
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="surrogateescape")
    if not isinstance(body, str):
        return body
    try:
        return json.loads(body)
    except ValueError:
        return body


def same_normalized_body(first: Any, second: Any) -> None:
    """VCR matcher: raising ``AssertionError`` is how a matcher says no."""
    assert normalized_body(first) == normalized_body(second)
