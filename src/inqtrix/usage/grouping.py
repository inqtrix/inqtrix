"""Group-by vocabulary shared by both usage-store twins.

One whitelist, one normalizer. The Postgres store and its memory twin must
accept exactly the same axes, or a deployment answers a cost question the
other cannot.
"""

from __future__ import annotations

USAGE_GROUP_KEYS = ("user_id", "model", "feature", "operation", "run_id")
"""Columns a caller may group ledger aggregates by."""

USAGE_GROUP_DEFAULT = ("model", "operation")
"""Default pair, because pricing needs both.

The price catalogue is chosen by operation and the rate by model; grouping
by either one alone cannot be priced.
"""


def normalize_usage_group_by(group_by: str | tuple[str, ...]) -> tuple[str, ...]:
    """Validate and de-duplicate a group-by request, preserving order.

    Args:
        group_by: One key or a tuple of keys from :data:`USAGE_GROUP_KEYS`.

    Returns:
        The requested keys without duplicates, in the order given.

    Raises:
        ValueError: If the request is empty or names an unsupported key. An
            unsupported key is rejected rather than dropped: silently
            grouping by less than asked would return a total that answers a
            different question than the caller posed.
    """
    keys = (group_by,) if isinstance(group_by, str) else tuple(group_by)
    if not keys:
        raise ValueError("group_by must name at least one key")
    unsupported = [key for key in keys if key not in USAGE_GROUP_KEYS]
    if unsupported:
        raise ValueError(
            f"unsupported group_by: {', '.join(unsupported)} "
            f"(supported: {', '.join(USAGE_GROUP_KEYS)})"
        )
    seen: dict[str, None] = {}
    for key in keys:
        seen.setdefault(key, None)
    return tuple(seen)


USAGE_PRICING_KEYS = ("model", "operation")
"""Axes the cost derivation needs on every row it prices.

The price catalogue is chosen by operation and the rate by model. A value
derived from two fields must never travel through a projection that can drop
one of them, so aggregation for costing always carries both — whatever the
caller asked to group the DISPLAY by.
"""


def costing_group_by(display_keys: tuple[str, ...]) -> tuple[str, ...]:
    """Widen a display grouping to the axes pricing needs.

    Args:
        display_keys: The normalized axes the caller wants to see.

    Returns:
        The display axes followed by any missing pricing axis, order stable so
        the display keys stay the row prefix.
    """
    missing = tuple(k for k in USAGE_PRICING_KEYS if k not in display_keys)
    return display_keys + missing
