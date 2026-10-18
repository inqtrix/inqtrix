"""Shared keyset (cursor) pagination for the native list endpoints.

One implementation, used by BOTH the in-memory and the Postgres tier of
every paginated list, so the two backends return byte-identical pages and
cursors for the same data (the same wire-parity invariant the SSE replay
discipline pins).

Design:

* Newest-first lists ordered by ``(created_at, id)``. The id is a
  mandatory tiebreaker — ``created_at`` is a float epoch that collides on
  bulk inserts, so a ``created_at``-only cursor would skip or repeat rows
  at a page edge.
* The cursor is OPAQUE: ``base64url(json({"ca": <created_at>, "id":
  <id>}))`` of the last row of the previous page. Clients pass it back
  verbatim via ``?cursor=``; they never construct it.
* The wire shape extends the existing ``{"object": "list", "data": [...]}``
  envelope with a sibling ``next_cursor`` (string or null) — additive, so
  callers that ignore it keep working. ``?limit=`` is optional and clamped.

The in-memory tier calls :func:`keyset_page` over its already
sorted-and-visibility-filtered list (filter BEFORE slice, so a page is
never under-filled). The Postgres tier decodes the cursor with
:func:`decode_cursor`, applies the keyset ``WHERE``/``ORDER BY``/``LIMIT``
itself, and builds the token with :func:`encode_cursor`. Both wrap the
result with :func:`list_envelope`.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any, Callable, Sequence, TypeVar

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

T = TypeVar("T")


class InvalidCursor(ValueError):
    """Raised when a client-supplied cursor token cannot be decoded.

    A malformed cursor is a client error (HTTP 400), never silently
    treated as "start from the beginning" — that would hide a real bug
    behind a wrong page (No-Silent-Fallbacks).
    """


def clamp_limit(raw: str | int | None) -> int:
    """Resolve and clamp a ``?limit=`` value to ``[1, MAX_PAGE_LIMIT]``.

    Absent/blank/unparseable falls back to :data:`DEFAULT_PAGE_LIMIT` (a
    missing limit is not a client error — it just means "use the default
    page size"); a parseable but out-of-range value is clamped, not
    rejected.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_PAGE_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_LIMIT
    return max(1, min(MAX_PAGE_LIMIT, value))


def encode_cursor(created_at: float, item_id: str) -> str:
    """Opaque base64url token for the ``(created_at, id)`` of a row."""
    payload = json.dumps({"ca": created_at, "id": item_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(token: str | None) -> tuple[float, str] | None:
    """Decode a cursor to ``(created_at, id)``; ``None`` when absent.

    Raises:
        InvalidCursor: The token is present but not a cursor this server
            issued (malformed base64/JSON or missing fields).
    """
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        data = json.loads(raw)
        return float(data["ca"]), str(data["id"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise InvalidCursor("malformed pagination cursor") from exc


def keyset_page(
    items: Sequence[T],
    *,
    limit: int,
    after: tuple[float, str] | None,
    created_at_of: Callable[[T], float],
    id_of: Callable[[T], str],
) -> tuple[list[T], str | None]:
    """Slice one keyset page out of a newest-first, already-filtered list.

    *items* must be sorted by ``(created_at, id)`` descending and already
    visibility-filtered (filter before slice keeps pages full). Returns the
    page (at most *limit* rows) and the ``next_cursor`` (``None`` on the
    last page).
    """
    if after is not None:
        items = [
            item
            for item in items
            if (created_at_of(item), id_of(item)) < after
        ]
    window = list(items[: limit + 1])
    page = window[:limit]
    next_cursor = (
        encode_cursor(created_at_of(page[-1]), id_of(page[-1]))
        if len(window) > limit and page
        else None
    )
    return page, next_cursor


def list_envelope(
    data: list[Any], next_cursor: str | None
) -> dict[str, Any]:
    """The paginated list wire shape: the existing envelope + next_cursor."""
    return {"object": "list", "data": data, "next_cursor": next_cursor}
