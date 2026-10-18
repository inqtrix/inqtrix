"""The knowledge scope a delegated child is submitted with.

A child is not admitted through the HTTP router, so the submission-time
collection pin never runs for it — and a missing pin fails closed to an
EMPTY boundary. Without inheritance every delegated mission would execute
with no knowledge at all while its surface reported knowledge as
available: it would see zero collections, ask the user for access it
already had, or answer from nothing.
"""

import pytest

from inqtrix.execution_authority import (
    inherited_knowledge_filters,
    pinned_knowledge_collection_ids,
)


def test_a_child_inherits_the_parents_collections() -> None:
    """The regression: without a pin the child has no knowledge."""
    filters = inherited_knowledge_filters(
        frozenset({"kc_b", "kc_a"}), explicit=False
    )
    assert filters["collection_ids"] == ["kc_a", "kc_b"]


def test_inheriting_is_not_widening() -> None:
    """A child never reaches past the boundary its parent was admitted
    with — the whole point of pinning the scope at submission."""
    parent = frozenset({"kc_a"})
    filters = inherited_knowledge_filters(parent, explicit=True)
    assert set(filters["collection_ids"]) == parent


def test_the_users_own_scope_choice_travels_with_it() -> None:
    """`explicit` decides how the balanced gate reads the search, so a
    child must not silently become an unscoped one."""
    assert inherited_knowledge_filters(frozenset({"kc_a"}), explicit=True)[
        "explicit"
    ] is True
    assert inherited_knowledge_filters(frozenset({"kc_a"}), explicit=False)[
        "explicit"
    ] is False


def test_an_empty_parent_boundary_is_inherited_as_empty() -> None:
    """A parent that legitimately reaches nothing hands down nothing —
    and says so with a pin, so the child does not fail closed by accident
    into the same state for a different reason."""
    filters = inherited_knowledge_filters(frozenset(), explicit=False)
    assert filters["collection_ids"] == []


def test_unscoped_execution_stays_unscoped() -> None:
    """``None`` means "no per-user sharing boundary" (anonymous/static
    library). That is not an empty boundary and must not become one."""
    assert inherited_knowledge_filters(None, explicit=False) == {}


@pytest.mark.parametrize("explicit", [True, False])
def test_the_inherited_pin_survives_the_round_trip(explicit: bool) -> None:
    """What the child inherits must read back as the same boundary — the
    contract that failed before: no pin at all read back as EMPTY."""
    parent = frozenset({"kc_a", "kc_b"})
    filters = inherited_knowledge_filters(parent, explicit=explicit)
    assert (
        pinned_knowledge_collection_ids(filters, scoped_principal=True)
        == parent
    )


def test_no_pin_still_fails_closed() -> None:
    """The guard inheritance relies on stays as it is: a scoped principal
    with no pin gets an empty boundary, never a wide one."""
    assert pinned_knowledge_collection_ids({}, scoped_principal=True) == (
        frozenset()
    )
