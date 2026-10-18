"""Unit tests for the shared keyset-pagination helper.

The helper is the single implementation both the in-memory and Postgres
list tiers use, so its cursor codec and slicing are the wire contract.
The load-bearing property is the ``id`` tiebreaker: ``created_at`` is a
float epoch that collides, and a page boundary must neither skip nor
repeat a row across the tie.
"""

from __future__ import annotations

import pytest

from inqtrix.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    InvalidCursor,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_page,
    list_envelope,
)


def test_cursor_round_trips():
    token = encode_cursor(1781682190.5317028, "kd_abc")
    assert decode_cursor(token) == (1781682190.5317028, "kd_abc")


def test_decode_absent_is_none_and_malformed_raises():
    assert decode_cursor(None) is None
    assert decode_cursor("") is None
    with pytest.raises(InvalidCursor):
        decode_cursor("not-a-cursor!!")


def test_clamp_limit_defaults_and_bounds():
    assert clamp_limit(None) == DEFAULT_PAGE_LIMIT
    assert clamp_limit("") == DEFAULT_PAGE_LIMIT
    assert clamp_limit("garbage") == DEFAULT_PAGE_LIMIT
    assert clamp_limit("10") == 10
    assert clamp_limit(0) == 1
    assert clamp_limit(9999) == MAX_PAGE_LIMIT


def _row(ca: float, rid: str) -> dict:
    return {"created_at": ca, "id": rid}


def _sorted_desc(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r["created_at"], r["id"]), reverse=True)


def test_keyset_page_walks_all_rows_without_skip_or_repeat():
    rows = _sorted_desc([_row(i, f"id{i:02d}") for i in range(10)])
    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous bound
        page, next_cursor = keyset_page(
            rows,
            limit=3,
            after=decode_cursor(cursor),
            created_at_of=lambda r: r["created_at"],
            id_of=lambda r: r["id"],
        )
        seen.extend(r["id"] for r in page)
        if next_cursor is None:
            break
        cursor = next_cursor
    # Every row exactly once, newest-first.
    assert seen == [r["id"] for r in rows]
    assert len(seen) == len(set(seen)) == 10


def test_keyset_page_id_tiebreaker_on_equal_created_at():
    # All four share created_at — only the id keeps the boundary stable.
    rows = _sorted_desc([_row(100.0, f"id{i}") for i in range(4)])
    page1, cursor = keyset_page(
        rows, limit=2, after=None,
        created_at_of=lambda r: r["created_at"], id_of=lambda r: r["id"],
    )
    assert cursor is not None
    page2, cursor2 = keyset_page(
        rows, limit=2, after=decode_cursor(cursor),
        created_at_of=lambda r: r["created_at"], id_of=lambda r: r["id"],
    )
    ids = [r["id"] for r in page1] + [r["id"] for r in page2]
    assert ids == [r["id"] for r in rows]  # no skip/repeat despite the tie
    assert cursor2 is None  # exhausted


def test_keyset_page_last_page_has_no_cursor():
    rows = _sorted_desc([_row(1, "a"), _row(2, "b")])
    page, cursor = keyset_page(
        rows, limit=5, after=None,
        created_at_of=lambda r: r["created_at"], id_of=lambda r: r["id"],
    )
    assert len(page) == 2
    assert cursor is None


def test_list_envelope_is_additive_over_the_existing_shape():
    env = list_envelope([{"id": "x"}], "tok")
    assert env == {"object": "list", "data": [{"id": "x"}], "next_cursor": "tok"}
